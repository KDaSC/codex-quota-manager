import Foundation

public struct QuotaWindow: Codable, Sendable, Identifiable {
    public var kind: String? = nil
    public var id: String { kind ?? String(durationMinutes) }
    public var usedPercent: Double
    public var durationMinutes: Int
    public var resetsAt: Date?
    public init(usedPercent: Double, durationMinutes: Int, resetsAt: Date?, kind: String? = nil) {
        self.kind = kind
        self.usedPercent = usedPercent; self.durationMinutes = durationMinutes; self.resetsAt = resetsAt
    }
    public var remainingPercent: Double { max(0, min(100, 100 - usedPercent)) }
}

public struct QuotaSnapshot: Codable, Sendable {
    public enum Source: String, Codable, Sendable { case simulation, live }
    public var accountKey: String
    public var collectedAt: Date
    public var source: Source
    public var windows: [QuotaWindow]
    public var limited: Bool

    public init(accountKey: String, collectedAt: Date, source: Source, windows: [QuotaWindow], limited: Bool = false) {
        self.accountKey = accountKey; self.collectedAt = collectedAt; self.source = source
        self.windows = windows; self.limited = limited
    }
    public var permitsLiveExecution: Bool { source == .live }
    public func hasManagedWindows(at now: Date) -> Bool {
        let kinds = windows.compactMap(\.kind)
        return windows.count == 2 && Set(kinds) == Set(["primary", "secondary"]) &&
            windows.allSatisfy { window in window.resetsAt.map { $0 > now } ?? false }
    }
    public func isFresh(at now: Date) -> Bool {
        let age = now.timeIntervalSince(collectedAt)
        return !accountKey.isEmpty && age >= 0 && age <= 60 && !windows.isEmpty && windows.allSatisfy {
            $0.usedPercent.isFinite && $0.usedPercent >= 0 && $0.usedPercent <= 100 && $0.durationMinutes > 0
        }
    }
    public var nextReset: Date? {
        let blocked = windows.filter { $0.usedPercent >= 90 }
        guard !blocked.isEmpty, blocked.allSatisfy({ $0.resetsAt != nil }) else { return nil }
        return blocked.compactMap(\.resetsAt).max()
    }
}

public enum DispatchDecision: String, Codable, Sendable {
    case blocked, waitingQuota, waitingConnection, needsReconciliation, simulationReady, liveReady
}

public enum QuotaPolicy {
    public static func hasUnsettledOperation(_ state: InboxState) -> Bool {
        state.tasks.contains {
            [.preparing, .running, .uncertain].contains($0.status) || ($0.operation != nil && $0.operation?.phase != .finished)
        }
    }
    public static func decide(state: InboxState, task: IdeaTask, quota: QuotaSnapshot?, now: Date, minimumRemaining: Int = 10) -> DispatchDecision {
        guard !state.stopped, task.automatic else { return .blocked }
        if task.status == .uncertain { return .needsReconciliation }
        guard [.queued, .waitingQuota, .waitingConnection].contains(task.status) else { return .blocked }
        guard !hasUnsettledOperation(state) else { return .blocked }
        guard let quota, quota.isFresh(at: now) else { return .waitingConnection }
        guard (0...100).contains(minimumRemaining) else { return .waitingConnection }
        guard !quota.limited, quota.windows.allSatisfy({ $0.remainingPercent > Double(minimumRemaining) }) else { return .waitingQuota }
        return quota.permitsLiveExecution ? .liveReady : .simulationReady
    }
}

extension InboxStore {
    public func prepare(taskID: UUID, quota: QuotaSnapshot, now: Date,
                        model: String = "gpt-6.1-sol", effort: String = "medium", minimumRemaining: Int = 10) throws -> TaskOperation? {
        let task = try task(taskID)
        let decision = QuotaPolicy.decide(state: state, task: task, quota: quota, now: now, minimumRemaining: minimumRemaining)
        guard decision == .simulationReady || decision == .liveReady else { return nil }
        let operation = TaskOperation(model: model, effort: effort, source: quota.source.rawValue)
        try update(taskID) { task in
            task.operation = operation
            task.status = .preparing
            task.note = "已持久化操作身份，尚未确认服务端接受。"
        }
        return operation
    }
}

public enum ObservationSchedule {
    public static func next(hasPending: Bool, stopped: Bool, quota: QuotaSnapshot?, failures: Int, now: Date) -> Date? {
        guard hasPending, !stopped else { return nil }
        if failures > 0 {
            let delays: [TimeInterval] = [120, 300, 900, 3600]
            return now.addingTimeInterval(delays[min(failures - 1, 3)])
        }
        let fallback = now.addingTimeInterval(900)
        guard let reset = quota?.nextReset else { return fallback }
        return min(fallback, max(now.addingTimeInterval(120), reset.addingTimeInterval(60)))
    }
}
