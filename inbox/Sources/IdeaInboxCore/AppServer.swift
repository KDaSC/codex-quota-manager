import Foundation
import Darwin
import CryptoKit

public indirect enum JSONValue: Codable, Sendable {
    case object([String: JSONValue]), array([JSONValue]), string(String), number(Double), bool(Bool), null
    public init(from decoder: any Decoder) throws {
        let c = try decoder.singleValueContainer()
        if c.decodeNil() { self = .null }
        else if let v = try? c.decode(Bool.self) { self = .bool(v) }
        else if let v = try? c.decode(Double.self) { self = .number(v) }
        else if let v = try? c.decode(String.self) { self = .string(v) }
        else if let v = try? c.decode([JSONValue].self) { self = .array(v) }
        else { self = .object(try c.decode([String: JSONValue].self)) }
    }
    public func encode(to encoder: any Encoder) throws {
        var c = encoder.singleValueContainer()
        switch self {
        case .object(let v): try c.encode(v)
        case .array(let v): try c.encode(v)
        case .string(let v): try c.encode(v)
        case .number(let v): try c.encode(v)
        case .bool(let v): try c.encode(v)
        case .null: try c.encodeNil()
        }
    }
    public subscript(_ key: String) -> JSONValue {
        if case .object(let v) = self { return v[key] ?? .null }; return .null
    }
    public var text: String? { if case .string(let v) = self { return v }; return nil }
    public var number: Double? { if case .number(let v) = self { return v }; return nil }
    public var boolean: Bool? { if case .bool(let v) = self { return v }; return nil }
    public var array: [JSONValue] { if case .array(let v) = self { return v }; return [] }
    public var object: [String: JSONValue] { if case .object(let v) = self { return v }; return [:] }
    public var isNull: Bool { if case .null = self { return true }; return false }
}

public enum CodexFailure: Error, LocalizedError {
    case connection, timeout, protocolError, limited, login, observationChanged, needsInput, server(Int)
    public var errorDescription: String? {
        switch self {
        case .connection: "Codex 连接已断开，请重新核对。"
        case .timeout: "Codex 请求超时；发送结果需核对，不会自动重发。"
        case .protocolError: "Codex 返回了无法识别的协议数据。"
        case .limited: "额度不足，等待服务端额度恢复。"
        case .login: "请先在 Codex 完成 ChatGPT 登录。"
        case .observationChanged: "账户观察期间收到更新，需要重新核验。"
        case .needsInput: "任务请求了额外权限或工具操作，需要你处理。"
        case .server(let code): "Codex 服务返回错误（\(code)），请核对当前状态。"
        }
    }
    public static func classify(_ value: JSONValue) -> CodexFailure {
        let data = (try? JSONEncoder().encode(value)) ?? Data()
        let text = String(data: data, encoding: .utf8)?.lowercased() ?? ""
        if ["usage limit", "rate limit", "quota", "ratelimit", "usage_limit"].contains(where: { text.contains($0) }) { return .limited }
        if ["unauthorized", "not authenticated", "login required"].contains(where: { text.contains($0) }) { return .login }
        return .server(Int(value["code"].number ?? -1))
    }
}

public struct ModelChoice: Codable, Sendable, Identifiable {
    public var id: String
    public var name: String
    public var efforts: [String]
    public var defaultEffort: String?
}

public struct CodexObservation: Codable, Sendable {
    public var authenticated: Bool
    public var plan: String?
    public var models: [ModelChoice]
    public var quota: QuotaSnapshot?
}

// Protocol is independent of UI and never reads authentication files itself.
public actor CodexConnection {
    private let executable: URL
    private let arguments: [String]
    private var process: Process?
    private var input: FileHandle?
    private var output: FileHandle?
    private var buffer = Data()
    private var pending: [JSONValue] = []
    private var sequence = 0
    private var accountRevision = 0
    private var quotaRevision = 0
    private var observedAccountRevision = 0
    private var observedQuotaRevision = 0

    public static let draftArguments: [String] = ["app-server", "--listen", "stdio://"] +
        ["shell_tool", "unified_exec", "apps", "plugins", "browser_use", "computer_use", "view_image",
         "image_generation", "multi_agent", "multi_agent_v2", "memories", "hooks", "skill_search",
         "skill_mcp_dependency_install", "goals", "worktrees", "code_mode_host"].flatMap { ["--disable", $0] } +
        ["-c", "web_search=\"disabled\"", "-c", "mcp_servers={}", "-c", "project_doc_max_bytes=0",
         "-c", "forced_login_method=\"chatgpt\""]

    public init(executable: URL, arguments: [String] = CodexConnection.draftArguments) {
        self.executable = executable; self.arguments = arguments
    }
    public static func locateExecutable() throws -> URL {
        let paths = (ProcessInfo.processInfo.environment["PATH"] ?? "").split(separator: ":").map(String.init)
        let candidates = paths.map { $0 + "/codex" } + [
            "/Applications/ChatGPT.app/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex",
            "/Applications/Codex.app/Contents/Resources/codex"
        ]
        guard let path = candidates.first(where: { FileManager.default.isExecutableFile(atPath: $0) }) else {
            throw InboxError.message("未找到本机 Codex；记录功能仍可使用。")
        }
        return URL(fileURLWithPath: path)
    }
    public func connect(timeout: TimeInterval = 15) throws {
        if process != nil { return }
        let child = Process(), stdin = Pipe(), stdout = Pipe()
        child.executableURL = executable; child.arguments = arguments
        child.standardInput = stdin; child.standardOutput = stdout; child.standardError = FileHandle.nullDevice
        try child.run()
        process = child; input = stdin.fileHandleForWriting; output = stdout.fileHandleForReading
        _ = fcntl(stdin.fileHandleForWriting.fileDescriptor, F_SETNOSIGPIPE, 1)
        do {
            let fd = stdin.fileHandleForWriting.fileDescriptor
            let flags = fcntl(fd, F_GETFL)
            guard flags >= 0, fcntl(fd, F_SETFL, flags | O_NONBLOCK) == 0 else { throw CodexFailure.connection }
            _ = try call("initialize", params: ["clientInfo": .object(["name": .string("idea_inbox"), "title": .string("Idea Inbox"), "version": .string("0.1.0")])], timeout: timeout)
            try send(.object(["method": .string("initialized"), "params": .object([:])]), deadline: Date().addingTimeInterval(timeout))
        } catch { close(); throw error }
    }
    public func close() {
        try? input?.close(); try? output?.close()
        if let child = process, child.isRunning {
            child.terminate()
            let deadline = Date().addingTimeInterval(2)
            while child.isRunning && Date() < deadline { usleep(10_000) }
            if child.isRunning { kill(child.processIdentifier, SIGKILL) }
            child.waitUntilExit()
        }
        process = nil; input = nil; output = nil; buffer.removeAll(); pending.removeAll()
    }
    private func send(_ value: JSONValue, deadline: Date) throws {
        guard process?.isRunning == true, let input else { throw CodexFailure.connection }
        var data = try JSONEncoder().encode(value); data.append(10)
        guard data.count <= 2_097_152 else { throw CodexFailure.protocolError }
        try data.withUnsafeBytes { bytes in
            var offset = 0
            while offset < bytes.count {
                let remaining = deadline.timeIntervalSinceNow
                guard remaining > 0 else { throw CodexFailure.timeout }
                let count = Darwin.write(input.fileDescriptor, bytes.baseAddress!.advanced(by: offset), bytes.count - offset)
                if count > 0 { offset += count; continue }
                if count < 0 && errno == EINTR { continue }
                guard count < 0 && (errno == EAGAIN || errno == EWOULDBLOCK) else { throw CodexFailure.connection }
                var descriptor = pollfd(fd: input.fileDescriptor, events: Int16(POLLOUT), revents: 0)
                let result = poll(&descriptor, 1, Int32(min(remaining * 1000 + 1, 1000)))
                if result < 0 && errno != EINTR { throw CodexFailure.connection }
            }
        }
    }
    private func read(deadline: Date) throws -> JSONValue {
        guard let output else { throw CodexFailure.connection }
        while true {
            if let newline = buffer.firstIndex(of: 10) {
                let line = Data(buffer[..<newline]); buffer.removeSubrange(...newline)
                guard let value = try? JSONDecoder().decode(JSONValue.self, from: line) else { throw CodexFailure.protocolError }
                if value["method"].text == "account/updated" { accountRevision += 1 }
                if value["method"].text == "account/rateLimits/updated" { quotaRevision += 1 }
                return value
            }
            let remaining = deadline.timeIntervalSinceNow
            guard remaining > 0 else { throw CodexFailure.timeout }
            var descriptor = pollfd(fd: output.fileDescriptor, events: Int16(POLLIN), revents: 0)
            let result = poll(&descriptor, 1, Int32(min(remaining * 1000 + 1, 1000)))
            if result < 0 { if errno == EINTR { continue }; throw CodexFailure.connection }
            if result == 0 { continue }
            var bytes = [UInt8](repeating: 0, count: 8192)
            let count = Darwin.read(output.fileDescriptor, &bytes, bytes.count)
            guard count > 0 else { throw CodexFailure.connection }
            buffer.append(contentsOf: bytes.prefix(count))
            guard buffer.count <= 2_097_152 else { throw CodexFailure.protocolError }
        }
    }
    public func call(_ method: String, params: [String: JSONValue] = [:], timeout: TimeInterval = 15) throws -> JSONValue {
        let allowed = ["initialize", "account/read", "model/list", "account/rateLimits/read", "thread/start", "thread/resume", "thread/read", "turn/start", "turn/interrupt"]
        guard allowed.contains(method) else { throw CodexFailure.protocolError }
        sequence += 1; let id = sequence
        let deadline = Date().addingTimeInterval(timeout)
        try send(.object(["id": .number(Double(id)), "method": .string(method), "params": .object(params)]), deadline: deadline)
        while true {
            let message = try read(deadline: deadline)
            if message["method"].text != nil {
                guard message["id"].isNull else { throw CodexFailure.needsInput }
                pending.append(message)
                guard pending.count <= 1024 else { throw CodexFailure.protocolError }
                continue
            }
            guard message["id"].number == Double(id) else { throw CodexFailure.protocolError }
            if !message["error"].isNull { throw CodexFailure.classify(message["error"]) }
            return message["result"]
        }
    }
    public func nextEvent(timeout: TimeInterval = 1) throws -> JSONValue? {
        if !pending.isEmpty { return pending.removeFirst() }
        do {
            let event = try read(deadline: Date().addingTimeInterval(timeout))
            guard event["id"].isNull else { throw CodexFailure.needsInput }
            return event
        } catch CodexFailure.timeout { return nil }
    }
    public func observe() throws -> CodexObservation {
        // A startup account announcement can race the first read. Retry only
        // the read-only observation once, never a model turn.
        do { return try observeOnce() }
        catch CodexFailure.observationChanged { return try observeOnce() }
    }
    private func observeOnce() throws -> CodexObservation {
        let initialAccountRevision = accountRevision
        let account = try call("account/read", params: ["refreshToken": .bool(false)])["account"]
        let catalog = try call("model/list", params: ["limit": .number(50), "includeHidden": .bool(false)])
        guard catalog["nextCursor"].isNull else { throw InboxError.message("模型目录未读取完整，已停止自动派发。") }
        let models = catalog["data"].array.compactMap { item -> ModelChoice? in
            guard let id = item["model"].text ?? item["id"].text else { return nil }
            return ModelChoice(id: id, name: item["displayName"].text ?? id,
                               efforts: item["supportedReasoningEfforts"].array.compactMap { $0["reasoningEffort"].text },
                               defaultEffort: item["defaultReasoningEffort"].text)
        }
        let limits = try call("account/rateLimits/read")
        guard initialAccountRevision == accountRevision else { throw CodexFailure.observationChanged }
        let identity = limits["accountId"].text ?? account["email"].text ?? ""
        let key = identity.isEmpty ? "" : SHA256.hash(data: Data(identity.utf8)).map { String(format: "%02x", $0) }.joined()
        let buckets = limits["rateLimitsByLimitId"].object
        let bucket: JSONValue
        if let shared = buckets["codex"] { bucket = shared }
        else if buckets.isEmpty { bucket = limits["rateLimits"] }
        else { bucket = .null }
        let windows = ["primary", "secondary"].compactMap { name -> QuotaWindow? in
            let w = bucket[name]
            guard let used = w["usedPercent"].number, let duration = w["windowDurationMins"].number,
                  duration > 0, duration < Double(Int.max) else { return nil }
            return QuotaWindow(usedPercent: used, durationMinutes: Int(duration), resetsAt: w["resetsAt"].number.map { Date(timeIntervalSince1970: $0) }, kind: name)
        }
        let authenticated = account["type"].text == "chatgpt"
        let expectedWindows = ["primary", "secondary"].filter { !bucket[$0].isNull }.count
        let quota = windows.isEmpty || windows.count != expectedWindows || key.isEmpty || !authenticated ? nil : QuotaSnapshot(
            accountKey: key, collectedAt: Date(), source: .live, windows: windows,
            limited: limits["ordinaryUsageAllowed"].boolean == false || !bucket["rateLimitReachedType"].isNull)
        observedAccountRevision = accountRevision; observedQuotaRevision = quotaRevision
        return CodexObservation(authenticated: authenticated, plan: account["planType"].text, models: models, quota: quota)
    }

    public func observationChanges() throws -> (account: Bool, quota: Bool) {
        // Drain already queued and immediately available notifications before sending.
        var count = 0
        while try nextEvent(timeout: 0.005) != nil {
            count += 1
            guard count <= 1024 else { throw CodexFailure.protocolError }
        }
        return (accountRevision != observedAccountRevision, quotaRevision != observedQuotaRevision)
    }
}
