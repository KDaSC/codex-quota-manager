import Foundation
import Darwin

/// Shared protocol with codex_resume.queueing; no model calls or queue DB writes.
public final class SharedQueueGate {
    private let root: URL
    private let directory: URL?
    public var managed: Bool { directory != nil }

    public init(root: URL) throws {
        self.root = root.standardizedFileURL.resolvingSymlinksInPath()
        let marker = self.root.appendingPathComponent(".codex-resume-queue.json")
        var markerInfo = stat()
        if lstat(marker.path, &markerInfo) != 0 {
            guard errno == ENOENT else { throw InboxError.message("共享连接标记不可读；已禁止自动整理。") }
            directory = nil; return
        }
        let attrs = try FileManager.default.attributesOfItem(atPath: marker.path)
        guard attrs[.type] as? FileAttributeType == .typeRegular,
              (attrs[.size] as? NSNumber)?.intValue ?? 0 < 8192,
              let value = try JSONSerialization.jsonObject(with: Data(contentsOf: marker)) as? [String: Any],
              value["schemaVersion"] as? Int == 1,
              let path = value["directory"] as? String, path.hasPrefix("/") else {
            throw InboxError.message("共享队列连接信息无效；已禁止自动整理。")
        }
        let url = URL(fileURLWithPath: path, isDirectory: true).standardizedFileURL
        var info = stat()
        guard lstat(url.path, &info) == 0, info.st_mode & S_IFMT == S_IFDIR,
              info.st_uid == getuid(), info.st_mode & 0o077 == 0 else {
            throw InboxError.message("共享队列目录权限不安全或不可用；已禁止自动整理。")
        }
        directory = url
    }

    public func setPaused(_ paused: Bool) throws {
        guard let directory else { return }
        let file = directory.appendingPathComponent("queue.paused")
        if paused {
            let fd = open(file.path, O_CREAT | O_WRONLY | O_NOFOLLOW | O_CLOEXEC, 0o600)
            guard fd >= 0 else { throw InboxError.message("无法暂停共享队列。") }
            Darwin.close(fd)
        } else if FileManager.default.fileExists(atPath: file.path) {
            try FileManager.default.removeItem(at: file)
        }
    }

    public func acquire(taskID: UUID) throws -> SharedQueuePermit? {
        guard let directory else { return SharedQueuePermit(fd: -1, accountKey: nil, minimumRemaining: 10, validate: { true }) }
        let validate = { [root] in
            Self.valid(directory: directory, root: root, taskID: taskID)
        }
        guard validate() else { return nil }
        let fd = open(directory.appendingPathComponent("dispatch.lock").path,
                      O_CREAT | O_RDWR | O_NOFOLLOW | O_CLOEXEC, 0o600)
        guard fd >= 0 else { throw InboxError.message("无法打开共享执行锁。") }
        var info = stat()
        guard fstat(fd, &info) == 0, info.st_mode & S_IFMT == S_IFREG,
              info.st_uid == getuid(), info.st_mode & 0o077 == 0 else {
            Darwin.close(fd); throw InboxError.message("共享执行锁权限不安全。")
        }
        guard flock(fd, LOCK_EX | LOCK_NB) == 0 else { Darwin.close(fd); return nil }
        guard validate() else { Darwin.close(fd); return nil }
        guard let data = try? Data(contentsOf: directory.appendingPathComponent("inbox-grant.json")),
              let value = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any],
              let key = value["accountKey"] as? String, !key.isEmpty,
              let minimum = value["minimumRemaining"] as? Int, [20, 30, 40, 50].contains(minimum) else { Darwin.close(fd); return nil }
        return SharedQueuePermit(fd: fd, accountKey: key, minimumRemaining: minimum, validate: {
            guard validate(), let data = try? Data(contentsOf: directory.appendingPathComponent("inbox-grant.json")),
                  let current = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] else { return false }
            return current["minimumRemaining"] as? Int == minimum
        })
    }

    private static func valid(directory: URL, root: URL, taskID: UUID) -> Bool {
        if FileManager.default.fileExists(atPath: directory.appendingPathComponent("queue.paused").path) { return false }
        let file = directory.appendingPathComponent("inbox-grant.json")
        guard let attrs = try? FileManager.default.attributesOfItem(atPath: file.path),
              attrs[.type] as? FileAttributeType == .typeRegular,
              (attrs[.size] as? NSNumber)?.intValue ?? 0 < 8192,
              let data = try? Data(contentsOf: file),
              let value = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any],
              value["schemaVersion"] as? Int == 1, value["allowed"] as? Bool == true,
              let grantedRoot = value["root"] as? String,
              URL(fileURLWithPath: grantedRoot).standardizedFileURL.resolvingSymlinksInPath().path == root.path,
              let id = value["taskID"] as? String, UUID(uuidString: id) == taskID,
              let issued = value["issuedAt"] as? Double, let expiry = value["expiresAt"] as? Double else { return false }
        let now = Date().timeIntervalSince1970
        return issued.isFinite && expiry.isFinite && issued <= now && now < expiry && expiry - issued <= 60.1
    }
}

public final class SharedQueuePermit {
    private var fd: Int32
    private let accountKey: String?
    public let minimumRemaining: Int
    public var requiresBothWindows: Bool { accountKey != nil }
    private let validate: () -> Bool
    fileprivate init(fd: Int32, accountKey: String?, minimumRemaining: Int, validate: @escaping () -> Bool) {
        self.fd = fd; self.accountKey = accountKey; self.minimumRemaining = minimumRemaining; self.validate = validate
    }
    public func canDispatch(accountKey current: String? = nil) -> Bool {
        validate() && (accountKey == nil || current == nil || accountKey == current)
    }
    public func close() { if fd >= 0 { Darwin.close(fd); fd = -1 } }
    deinit { close() }
}
