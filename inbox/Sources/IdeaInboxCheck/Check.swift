import Foundation
import IdeaInboxCore

private let fixtureServerPath = URL(fileURLWithPath: #filePath)
    .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
    .appendingPathComponent("tests/fake_server.py").path

private func require(_ condition: Bool, _ message: String) throws {
    if !condition { throw InboxError.message(message) }
}

private func sample(now: Date, primary: Double = 20, secondary: Double = 30, age: Double = 0) -> QuotaSnapshot {
    QuotaSnapshot(accountKey: "test-account", collectedAt: now.addingTimeInterval(-age), source: .simulation,
                  windows: [QuotaWindow(usedPercent: primary, durationMinutes: 300, resetsAt: now.addingTimeInterval(3600)),
                            QuotaWindow(usedPercent: secondary, durationMinutes: 10080, resetsAt: now.addingTimeInterval(7200))])
}

@main struct Check {
    @MainActor static func main() async {
        var failed = 0
        var count = 0
        let now = Date()
        var cases: [(String, @MainActor () async throws -> Void)] = [
            ("共享队列要求五小时和周窗口", {
                var quota = sample(now: now)
                quota.windows[0].kind = "primary"; quota.windows[1].kind = "secondary"
                try require(quota.hasManagedWindows(at: now), "完整双窗口不可用")
                quota.windows.removeLast()
                try require(!quota.hasManagedWindows(at: now), "缺少周窗口却可派发")
            }),
            ("共享队列许可、互斥锁和总暂停", {
                let base = FileManager.default.temporaryDirectory.appendingPathComponent("queue-check-" + UUID().uuidString)
                let root = base.appendingPathComponent("inbox"), queue = base.appendingPathComponent("queue")
                defer { try? FileManager.default.removeItem(at: base) }
                for url in [root, queue] { try FileManager.default.createDirectory(at: url, withIntermediateDirectories: true, attributes: [.posixPermissions: 0o700]) }
                try JSONSerialization.data(withJSONObject: ["schemaVersion": 1, "directory": queue.path]).write(to: root.appendingPathComponent(".codex-resume-queue.json"))
                let gate = try SharedQueueGate(root: root), id = UUID()
                let blocked = try gate.acquire(taskID: id)
                try require(gate.managed && blocked == nil, "没有许可也获得执行槽")
                func grant(_ offset: Double, issued: Double = 0) throws {
                    let data: [String: Any] = ["schemaVersion": 1, "allowed": true, "root": root.path, "taskID": id.uuidString, "accountKey": "fixture", "minimumRemaining": 30,
                        "issuedAt": Date().addingTimeInterval(issued).timeIntervalSince1970,
                        "expiresAt": Date().addingTimeInterval(offset).timeIntervalSince1970]
                    try JSONSerialization.data(withJSONObject: data).write(to: queue.appendingPathComponent("inbox-grant.json"))
                }
                try grant(60)
                let permit = try gate.acquire(taskID: id)
                try require(permit != nil && permit?.minimumRemaining == 30, "有效许可或保留线未通过")
                try require(permit?.canDispatch(accountKey: "other-account") == false, "不同账户使用了许可")
                let duplicate = try gate.acquire(taskID: id), wrong = try gate.acquire(taskID: UUID())
                try require(duplicate == nil && wrong == nil, "并发或另一任务获得执行槽")
                try gate.setPaused(true)
                try require(permit?.canDispatch() == false, "派发前总暂停失效")
                permit?.close(); try gate.setPaused(false)
                try grant(-1)
                let expired = try gate.acquire(taskID: id)
                try grant(120, issued: 60)
                let future = try gate.acquire(taskID: id)
                try require(expired == nil && future == nil, "过期或未来许可被使用")
                try grant(60)
                let released = try gate.acquire(taskID: id)
                try require(released != nil, "释放后不能恢复")
                released?.close()
                let marker = root.appendingPathComponent(".codex-resume-queue.json")
                try FileManager.default.removeItem(at: marker)
                try FileManager.default.createSymbolicLink(atPath: marker.path, withDestinationPath: base.appendingPathComponent("missing").path)
                var rejected = false
                do { _ = try SharedQueueGate(root: root) } catch { rejected = true }
                try require(rejected, "损坏的连接标记被当作独立模式")
            }),
            ("自选保留线含收尾缓冲", {
                let task = IdeaTask(text: "想法", automatic: true)
                let quota = sample(now: now, primary: 70, secondary: 10)
                try require(QuotaPolicy.decide(state: InboxState(), task: task, quota: quota, now: now, minimumRemaining: 30) == .waitingQuota, "30%派发线未生效")
                let ready = sample(now: now, primary: 69, secondary: 69)
                try require(QuotaPolicy.decide(state: InboxState(), task: task, quota: ready, now: now, minimumRemaining: 30) == .simulationReady, "高于派发线不能继续")
            }),
            ("恰好剩余10%不派发", {
                let task = IdeaTask(text: "想法", automatic: true)
                try require(QuotaPolicy.decide(state: InboxState(), task: task, quota: sample(now: now, primary: 90), now: now) == .waitingQuota, "阈值错误")
            }),
            ("仅周窗口受限也等待", {
                let task = IdeaTask(text: "想法", automatic: true)
                try require(QuotaPolicy.decide(state: InboxState(), task: task, quota: sample(now: now, secondary: 91), now: now) == .waitingQuota, "周限额未拦截")
            }),
            ("两窗口剩余11%允许演练", {
                let task = IdeaTask(text: "想法", automatic: true)
                try require(QuotaPolicy.decide(state: InboxState(), task: task, quota: sample(now: now, primary: 89, secondary: 89), now: now) == .simulationReady, "应允许演练")
            }),
            ("模拟不能进入真实执行", {
                try require(!sample(now: now).permitsLiveExecution, "模拟被当作真实额度")
            }),
            ("过期快照要求复查", {
                let task = IdeaTask(text: "想法", automatic: true)
                try require(QuotaPolicy.decide(state: InboxState(), task: task, quota: sample(now: now, age: 61), now: now) == .waitingConnection, "旧快照被使用")
            }),
            ("未知额度要求复查", {
                try require(QuotaPolicy.decide(state: InboxState(), task: IdeaTask(text: "想法", automatic: true), quota: nil, now: now) == .waitingConnection, "未知额度被使用")
            }),
            ("只记录不进入队列", {
                try require(QuotaPolicy.decide(state: InboxState(), task: IdeaTask(text: "想法", automatic: false), quota: sample(now: now), now: now) == .blocked, "记录被执行")
            }),
            ("总停止与取消优先", {
                var state = InboxState(); state.stopped = true
                var task = IdeaTask(text: "想法", automatic: true)
                try require(QuotaPolicy.decide(state: state, task: task, quota: sample(now: now), now: now) == .blocked, "总停止失效")
                state.stopped = false; task.status = .cancelled
                try require(QuotaPolicy.decide(state: state, task: task, quota: sample(now: now), now: now) == .blocked, "取消失效")
            }),
            ("账号变化使快照失效", {
                var quota = sample(now: now); quota.accountKey = ""
                try require(QuotaPolicy.decide(state: InboxState(), task: IdeaTask(text: "想法", automatic: true), quota: quota, now: now) == .waitingConnection, "账号无效未拦截")
            }),
            ("重复准备只有一个操作", {
                let root = FileManager.default.temporaryDirectory.appendingPathComponent("idea-check-" + UUID().uuidString)
                defer { try? FileManager.default.removeItem(at: root) }
                let store = try InboxStore(root: root)
                let task = try store.capture(text: "想法", automatic: true)
                let a = try store.prepare(taskID: task.id, quota: sample(now: now), now: now)
                let b = try store.prepare(taskID: task.id, quota: sample(now: now), now: now)
                try require(a != nil && b == nil, "重复请求获得了派发许可")
            }),
            ("重启后发送中操作不重放", {
                let root = FileManager.default.temporaryDirectory.appendingPathComponent("idea-check-" + UUID().uuidString)
                defer { try? FileManager.default.removeItem(at: root) }
                let id: UUID
                do {
                    let store = try InboxStore(root: root)
                    id = try store.capture(text: "想法", automatic: true).id
                    _ = try store.prepare(taskID: id, quota: sample(now: now), now: now)
                }
                let reopened = try InboxStore(root: root)
                let task = try reopened.task(id)
                try require(task.status == .uncertain, "重启后应核对")
                try require(QuotaPolicy.decide(state: reopened.state, task: task, quota: sample(now: now), now: now) == .needsReconciliation, "不确定状态被重复执行")
            }),
            ("并发第二个任务被拦截", {
                var state = InboxState()
                var active = IdeaTask(text: "运行", automatic: true); active.status = .running
                state.tasks = [active]
                try require(QuotaPolicy.decide(state: state, task: IdeaTask(text: "等待", automatic: true), quota: sample(now: now), now: now) == .blocked, "并发闸门失效")
            }),
            ("两个受限窗口取较晚重置", {
                let quota = sample(now: now, primary: 99, secondary: 99)
                try require(quota.nextReset == now.addingTimeInterval(7200), "重置时刻错误")
            }),
            ("非数字额度与未来采集时间无效", {
                var quota = sample(now: now); quota.windows[0].usedPercent = .nan
                try require(!quota.isFresh(at: now), "非数字额度被接受")
                quota = sample(now: now); quota.collectedAt = now.addingTimeInterval(10)
                try require(!quota.isFresh(at: now), "未来快照被接受")
            }),
            ("协议分片与只读摘要", {
                let connection = CodexConnection(executable: URL(fileURLWithPath: "/usr/bin/python3"), arguments: [fixtureServerPath, "fragmented"])
                try await connection.connect(timeout: 2)
                let observation = try await connection.observe()
                await connection.close()
                try require(observation.authenticated && observation.models.count == 1, "摘要解析失败")
                try require(observation.quota?.windows.count == 2, "额度解析失败")
                let report = String(data: try InboxJSON.encode(observation), encoding: .utf8)!
                try require(!report.contains("fixture@example.invalid") && !report.contains("fixture-account"), "身份原文泄露")
            }),
            ("协议EOF及时失败", {
                let connection = CodexConnection(executable: URL(fileURLWithPath: "/usr/bin/python3"), arguments: [fixtureServerPath, "eof"])
                var rejected = false
                do { try await connection.connect(timeout: 1) } catch { rejected = true }
                await connection.close()
                try require(rejected, "EOF未失败")
            }),
            ("协议超时有界", {
                let connection = CodexConnection(executable: URL(fileURLWithPath: "/usr/bin/python3"), arguments: [fixtureServerPath, "timeout"])
                let start = Date()
                var rejected = false
                do { try await connection.connect(timeout: 0.2) } catch { rejected = true }
                await connection.close()
                try require(rejected && Date().timeIntervalSince(start) < 4, "超时未受限")
            }),
            ("缺失额度不能当作零用量", {
                let connection = CodexConnection(executable: URL(fileURLWithPath: "/usr/bin/python3"), arguments: [fixtureServerPath, "missing-quota"])
                try await connection.connect(timeout: 2)
                let observation = try await connection.observe()
                await connection.close()
                try require(observation.quota == nil, "缺失额度被补零")
            }),
            ("已出现的周窗口缺字段必须等待", {
                let connection = CodexConnection(executable: URL(fileURLWithPath: "/usr/bin/python3"), arguments: [fixtureServerPath, "invalid-secondary"])
                try await connection.connect(timeout: 2)
                let observation = try await connection.observe()
                await connection.close()
                try require(observation.quota == nil, "坏周窗口被静默忽略")
            })
        ]
        cases.append(("等待与退避定时不会高频请求", {
            try require(ObservationSchedule.next(hasPending: false, stopped: false, quota: nil, failures: 0, now: now) == nil, "空闲仍轮询")
            try require(ObservationSchedule.next(hasPending: true, stopped: true, quota: nil, failures: 0, now: now) == nil, "停止仍轮询")
            try require(ObservationSchedule.next(hasPending: true, stopped: false, quota: nil, failures: 1, now: now) == now.addingTimeInterval(120), "退避错误")
            try require(ObservationSchedule.next(hasPending: true, stopped: false, quota: nil, failures: 4, now: now) == now.addingTimeInterval(3600), "持续失败仍高频")
            var quota = sample(now: now, primary: 95); quota.windows[0].resetsAt = now.addingTimeInterval(200)
            try require(ObservationSchedule.next(hasPending: true, stopped: false, quota: quota, failures: 0, now: now) == now.addingTimeInterval(260), "重置后缓冲错误")
        }))
        cases.append(("写管道背压也有超时", {
            let c = CodexConnection(executable: URL(fileURLWithPath: "/usr/bin/python3"), arguments: [fixtureServerPath, "backpressure"])
            try await c.connect(timeout: 1)
            let start = Date()
            var failed = false
            do { _ = try await c.call("thread/start", params: ["baseInstructions": .string(String(repeating: "x", count: 1_000_000))], timeout: 0.15) }
            catch { failed = true }
            await c.close()
            try require(failed && Date().timeIntervalSince(start) < 3, "写入阻塞超过截止时间")
        }))
        cases.append(("初始账号通知后重读完整观察", {
            let c = CodexConnection(executable: URL(fileURLWithPath: "/usr/bin/python3"), arguments: [fixtureServerPath, "initial-account"])
            try await c.connect(timeout: 1)
            let observation = try await c.observe()
            await c.close()
            try require(observation.authenticated && observation.quota != nil, "初始通知被当作登录失效")
        }))
        for scenario in ["normal", "limited", "send-timeout", "tools", "negative-quota", "unknown-bucket", "quota-before-send", "account-before-send", "read-quota"] {
            cases.append(("任务执行路径-" + scenario, {
                let root = FileManager.default.temporaryDirectory.appendingPathComponent("idea-worker-" + UUID().uuidString)
                defer { try? FileManager.default.removeItem(at: root) }
                let store = try InboxStore(root: root)
                let task = try store.capture(text: "做一个想法记录器", automatic: true)
                let log = root.appendingPathComponent("requests.log").path
                let worker = InboxWorker(store: store, requestTimeout: scenario == "send-timeout" ? 0.2 : 2,
                                         connectionFactory: { CodexConnection(executable: URL(fileURLWithPath: "/usr/bin/python3"), arguments: [fixtureServerPath, scenario, log]) })
                async let first: Void = worker.runNext()
                async let second: Void = worker.runNext()
                _ = await (first, second)
                await worker.shutdown()
                let result = try store.task(task.id)
                let noDispatch = ["negative-quota", "unknown-bucket", "quota-before-send", "account-before-send"].contains(scenario)
                let expected: TaskStatus = scenario == "normal" ? .completed : scenario == "limited" ? .waitingQuota : scenario == "tools" ? .needsInput : noDispatch ? .waitingConnection : .uncertain
                try require(result.status == expected, "任务状态错误：\(result.status)")
                let methods = (try? String(contentsOfFile: log, encoding: .utf8)) ?? ""
                try require(methods.split(separator: "\n").filter { $0 == "turn/start" }.count == (noDispatch ? 0 : 1), "错误派发数量")
                if scenario == "account-before-send" { try require(store.state.stopped, "账户变化未暂停") }
                if scenario == "normal" {
                    try require(result.draft?.contains("# 目标") == true, "未读回草稿")
                    try require(FileManager.default.fileExists(atPath: root.appendingPathComponent("tasks/\(task.id.uuidString)/草稿.md").path), "缺少草稿文件")
                }
                if scenario == "send-timeout" || scenario == "tools" {
                    _ = try store.capture(text: "第二条不得执行", automatic: true)
                    await worker.runNext()
                    let after = (try? String(contentsOfFile: log, encoding: .utf8)) ?? ""
                    try require(after == methods, "未知结果被重放")
                }
                if scenario == "read-quota" {
                    await worker.runNext()
                    await worker.reconcile(task.id)
                    await worker.shutdown()
                    try require(try store.task(task.id).status == .completed, "无法核对已完成草稿")
                    let after = try String(contentsOfFile: log, encoding: .utf8)
                    try require(after.split(separator: "\n").filter { $0 == "turn/start" }.count == 1, "读回失败导致重新执行")
                }
            }))
        }
        cases.append(("草稿目录故障后本机恢复读回结果", {
            let root = FileManager.default.temporaryDirectory.appendingPathComponent("idea-draft-recovery-" + UUID().uuidString)
            defer { try? FileManager.default.removeItem(at: root) }
            let store = try InboxStore(root: root)
            let task = try store.capture(text: "测试草稿恢复", automatic: true)
            let output = root.appendingPathComponent("bad-output", isDirectory: true)
            try FileManager.default.createDirectory(at: output.appendingPathComponent("tasks", isDirectory: true), withIntermediateDirectories: true)
            try Data("conflict".utf8).write(to: output.appendingPathComponent("tasks/\(task.id.uuidString)"))
            try store.update(task.id) { $0.workspacePath = output.path }
            let log = root.appendingPathComponent("requests.log").path
            let worker = InboxWorker(store: store, requestTimeout: 2, connectionFactory: {
                CodexConnection(executable: URL(fileURLWithPath: "/usr/bin/python3"), arguments: [fixtureServerPath, "normal", log])
            })
            await worker.runNext()
            var result = try store.task(task.id)
            try require(result.status == .uncertain && result.draft?.contains("# 目标") == true, "目标写入失败时没有保留已读回的草稿")
            let recovered = root.appendingPathComponent("recovered", isDirectory: true)
            try FileManager.default.createDirectory(at: recovered, withIntermediateDirectories: true)
            try worker.saveRecoveredDraft(task.id, to: recovered)
            result = try store.task(task.id)
            try require(result.status == .completed && result.draftPath != nil, "本机改选位置后没有完成保存")
            try require((try String(contentsOfFile: result.draftPath!, encoding: .utf8)).contains("# 目标"), "恢复写入的草稿内容不正确")
            let methods = try String(contentsOfFile: log, encoding: .utf8)
            try require(methods.split(separator: "\n").filter { $0 == "turn/start" }.count == 1, "本机恢复重复调用模型")
            await worker.shutdown()
        }))
        for (name, check) in cases {
            count += 1
            do { try await check(); print("PASS \(name)") }
            catch { failed += 1; print("FAIL \(name): \(error.localizedDescription)") }
        }
        print("\(count - failed)/\(count) passed")
        if failed > 0 { exit(1) }
    }
}
