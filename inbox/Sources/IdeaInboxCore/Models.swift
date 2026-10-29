import Foundation

public enum InboxError: Error, LocalizedError {
    case message(String)
    public var errorDescription: String? {
        if case .message(let value) = self { return value }
        return nil
    }
}

public enum TaskStatus: String, Codable, Sendable {
    case recorded, queued, preparing, running, waitingQuota, waitingConnection
    case needsInput, uncertain, completed, cancelled

    public var label: String {
        switch self {
        case .recorded: "已记录"
        case .queued: "等待整理"
        case .preparing: "准备中"
        case .running: "整理中"
        case .waitingQuota: "等待额度恢复"
        case .waitingConnection: "等待连接"
        case .needsInput: "需要你处理"
        case .uncertain: "需要核对执行结果"
        case .completed: "草稿已完成"
        case .cancelled: "已取消"
        }
    }
}

public struct IdeaTask: Codable, Identifiable, Sendable {
    public var id = UUID()
    public var text: String
    public var createdAt = Date()
    public var updatedAt = Date()
    public var automatic: Bool
    public var status: TaskStatus
    public var milestone = "把原始想法整理为目标、约束和下一步建议"
    public var note: String?
    public var draft: String?
    public var threadID: String?
    public var operation: TaskOperation?
    public var workspacePath: String?
    public var draftPath: String?
    public var attachedFiles: [IdeaAttachment]?

    public init(text: String, automatic: Bool, workspacePath: String? = nil,
                attachedFiles: [IdeaAttachment] = []) {
        self.text = text
        self.automatic = automatic
        status = automatic ? .queued : .recorded
        self.workspacePath = workspacePath
        self.attachedFiles = attachedFiles.isEmpty ? nil : attachedFiles
    }
}

/// Text explicitly selected in Finder. The path is discarded; only the filename
/// and private local snapshot remain with the captured idea.
public struct IdeaAttachment: Codable, Identifiable, Sendable {
    public var id = UUID()
    public var name: String
    public var text: String
    public init(name: String, text: String) { self.name = name; self.text = text }
}

public struct TaskOperation: Codable, Sendable {
    public enum Phase: String, Codable, Sendable { case prepared, sending, accepted, finished, uncertain }
    public var id = UUID()
    public var createdAt = Date()
    public var phase: Phase = .prepared
    public var turnID: String?
    public var model: String
    public var effort: String
    public var source: String
    public var retries = 0

    public init(model: String, effort: String, source: String) {
        self.model = model
        self.effort = effort
        self.source = source
    }
}

public struct InboxState: Codable, Sendable {
    public var schemaVersion = 1
    public var stopped = false
    public var tasks: [IdeaTask] = []
    public var accountKey: String?
    public var cachedQuota: QuotaSnapshot?
    public init() {}
}

public enum InboxJSON {
    public static func encode<T: Encodable>(_ value: T) throws -> Data {
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.prettyPrinted, .sortedKeys, .withoutEscapingSlashes]
        encoder.dateEncodingStrategy = .iso8601
        return try encoder.encode(value)
    }
    public static func decode<T: Decodable>(_ type: T.Type, from data: Data) throws -> T {
        let decoder = JSONDecoder()
        decoder.dateDecodingStrategy = .iso8601
        return try decoder.decode(type, from: data)
    }
}
