import Foundation
import Darwin

private final class RootLock {
    private let fd: Int32

    init(root: URL) throws {
        fd = open(root.appendingPathComponent(".lock").path, O_CREAT | O_RDWR | O_NOFOLLOW | O_CLOEXEC, 0o600)
        guard fd >= 0 else { throw InboxError.message("无法创建数据目录锁。请核对目录权限。") }
        guard flock(fd, LOCK_EX | LOCK_NB) == 0 else {
            close(fd)
            throw InboxError.message("这个数据目录已被另一个实例打开。请先退出该实例。")
        }
    }
    deinit { flock(fd, LOCK_UN); close(fd) }
}

// ponytail: one snapshot and one writer; introduce a database only after measured scale requires it.
public final class InboxStore {
    public let root: URL
    public private(set) var state: InboxState
    private let lock: RootLock
    private let file: URL
    private var savedData: Data?

    public init(root: URL) throws {
        let fm = FileManager.default
        try fm.createDirectory(at: root, withIntermediateDirectories: true, attributes: [.posixPermissions: 0o700])
        self.root = root.resolvingSymlinksInPath().standardizedFileURL
        lock = try RootLock(root: self.root)
        file = self.root.appendingPathComponent("state.json")
        if fm.fileExists(atPath: file.path) {
            do {
                let attrs = try fm.attributesOfItem(atPath: file.path)
                guard attrs[.type] as? FileAttributeType == .typeRegular else {
                    throw InboxError.message("状态文件必须是普通文件。")
                }
                let data = try Data(contentsOf: file)
                guard let json = try JSONSerialization.jsonObject(with: data) as? [String: Any],
                      json["schemaVersion"] as? Int == 1 else {
                    throw InboxError.message("状态损坏或版本不支持，已保留原文件；请核对 state.backup.json。")
                }
                state = try InboxJSON.decode(InboxState.self, from: data)
                savedData = data
            } catch {
                throw InboxError.message("状态无法读取，已保留原文件；请核对 state.backup.json。")
            }
        } else {
            state = InboxState()
        }
        try Self.validate(state)
        // A restart is evidence of neither acceptance nor completion.
        if state.tasks.contains(where: { $0.status == .preparing || $0.status == .running }) {
            try transaction { next in
                for i in next.tasks.indices where next.tasks[i].status == .preparing || next.tasks[i].status == .running {
                    next.tasks[i].status = .uncertain
                    next.tasks[i].operation?.phase = .uncertain
                    next.tasks[i].note = "程序曾中断；先核对原回合，不能自动重发。"
                }
            }
        }
    }

    private static func validate(_ state: InboxState) throws {
        guard state.schemaVersion == 1, Set(state.tasks.map(\.id)).count == state.tasks.count,
              state.tasks.allSatisfy({ !$0.text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty }) else {
            throw InboxError.message("状态校验失败，原文件保持不变。")
        }
    }

    public func transaction(_ change: (inout InboxState) throws -> Void) throws {
        var next = state
        try change(&next)
        try Self.validate(next)
        let data = try InboxJSON.encode(next)
        let backup = root.appendingPathComponent("state.backup.json")
        try writePrivate(savedData ?? data, to: backup)
        try writePrivate(data, to: file)
        savedData = data
        state = next
    }

    private func writePrivate(_ data: Data, to url: URL) throws {
        try data.write(to: url, options: [.atomic])
        try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: url.path)
    }

    @discardableResult public func capture(text: String, automatic: Bool = false,
                                           workspacePath: String? = nil,
                                           attachedFiles: [IdeaAttachment] = []) throws -> IdeaTask {
        guard !text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else {
            throw InboxError.message("请先输入想法。")
        }
        let task = IdeaTask(text: text, automatic: automatic, workspacePath: workspacePath, attachedFiles: attachedFiles)
        try transaction { $0.tasks.append(task) }
        return task
    }

    public func task(_ id: UUID) throws -> IdeaTask {
        guard let task = state.tasks.first(where: { $0.id == id }) else { throw InboxError.message("找不到这个任务。") }
        return task
    }

    public func update(_ id: UUID, _ change: (inout IdeaTask) throws -> Void) throws {
        try transaction { next in
            guard let i = next.tasks.firstIndex(where: { $0.id == id }) else { throw InboxError.message("找不到这个任务。") }
            try change(&next.tasks[i])
            next.tasks[i].updatedAt = Date()
        }
    }

    public func setStopped(_ stopped: Bool) throws { try transaction { $0.stopped = stopped } }
    public func cancel(_ id: UUID) throws {
        try update(id) { task in
            guard ![.running, .preparing, .uncertain].contains(task.status),
                  task.operation == nil || task.operation?.phase == .finished else {
                throw InboxError.message("任务正在执行；请先停止并核对回合。")
            }
            task.status = .cancelled
            task.note = "用户取消；不会因额度恢复而重新开始。"
        }
    }
}
