// AI Monitor — native menubar app for Claude Code + OpenAI Codex CLI usage.
//
// Reads /tmp/ai-monitor/state.json, which is written by the Python data
// layer (`monitor refresh` from the ai_monitor package). The Swift side
// owns: polling (timer fires the subprocess), parsing the JSON, and the UI.
//
// Single file; build via swift/build.sh.

import AppKit
import Combine
import Foundation
import SwiftUI

// MARK: - State model (matches /tmp/ai-monitor/state.json)

struct MonitorState: Codable {
    let generatedAt: Int
    let agents: [AgentState]

    enum CodingKeys: String, CodingKey {
        case generatedAt = "generated_at"
        case agents
    }
}

struct AgentState: Codable, Identifiable {
    let id: String
    let label: String
    let window: LimitWindow?
    let secondaryWindows: [LimitWindow]
    let extraCredits: RemoteUsage?
    let threads: [ThreadInfo]
    let byModel: [ModelTotal]
    let byProject: [ProjectTotal]
    let processesNoSid: [ProcessRollup]
    let errors: [AgentError]
    let cacheAges: [String: Int]

    enum CodingKeys: String, CodingKey {
        case id, label, window, threads, errors
        case secondaryWindows = "secondary_windows"
        case extraCredits = "extra_credits"
        case byModel = "by_model"
        case byProject = "by_project"
        case processesNoSid = "processes_no_sid"
        case cacheAges = "cache_ages"
    }
}

struct LimitWindow: Codable {
    let kind: String       // "rolling_5h" / "rolling_7d" / "weekly"
    let pct: Int
    let resetsAt: String?  // ISO8601
    let billable: Int
    let cap: Int

    enum CodingKeys: String, CodingKey {
        case kind, pct, billable, cap
        case resetsAt = "resets_at"
    }
}

struct RemoteUsage: Codable {
    let pct: Int
    let used: String
    let limit: String
    let ccy: String
}

struct ThreadInfo: Codable, Identifiable {
    let sid: String
    let project: String
    let billable: Int
    let pid: Int?
    let active: Bool
    let title: String?
    let firstMsg: String?
    let branch: String?
    let contextPct: Int?
    let contextTokens: Int?
    let contextMax: Int?

    var id: String { sid }

    enum CodingKeys: String, CodingKey {
        case sid, project, billable, pid, active, title, branch
        case firstMsg = "first_msg"
        case contextPct = "context_pct"
        case contextTokens = "context_tokens"
        case contextMax = "context_max"
    }
}

struct ModelTotal: Codable {
    let name: String
    let billable: Int
}

struct ProjectTotal: Codable {
    let name: String
    let billable: Int
}

struct ProcessRollup: Codable {
    let entry: String
    let project: String
    let count: Int
}

struct AgentError: Codable {
    let source: String
    let code: String
    let at: Int
}

// MARK: - Refresher

@MainActor
final class Refresher: ObservableObject {
    @Published var state: MonitorState?
    @Published var lastError: String?
    @Published var lastUpdated: Date?
    @Published var isRefreshing = false

    private var timer: Timer?
    private let stateFile = "/tmp/ai-monitor/state.json"
    private let pollInterval: TimeInterval = 30

    func start() {
        refresh()  // immediate
        timer = Timer.scheduledTimer(withTimeInterval: pollInterval, repeats: true) { [weak self] _ in
            Task { @MainActor in self?.refresh() }
        }
    }

    func stop() {
        timer?.invalidate()
        timer = nil
    }

    func refresh() {
        guard !isRefreshing else { return }
        isRefreshing = true

        Task.detached(priority: .background) { [weak self] in
            guard let self = self else { return }
            await self.runMonitorRefresh()
            await self.loadStateFile()
            await MainActor.run {
                self.lastUpdated = Date()
                self.isRefreshing = false
            }
        }
    }

    private func runMonitorRefresh() async {
        // Try the user-site script first, then fall back to PATH lookup.
        let candidatePaths = [
            NSHomeDirectory() + "/Library/Python/3.13/bin/monitor",
            "/usr/local/bin/monitor",
            "/opt/homebrew/bin/monitor",
        ]
        let monitorPath = candidatePaths.first(where: { FileManager.default.isExecutableFile(atPath: $0) })

        let proc = Process()
        if let path = monitorPath {
            proc.executableURL = URL(fileURLWithPath: path)
            proc.arguments = ["refresh"]
        } else {
            // Fall back to env lookup so the user gets a clear error if monitor isn't on PATH.
            proc.executableURL = URL(fileURLWithPath: "/usr/bin/env")
            proc.arguments = ["monitor", "refresh"]
        }

        let errPipe = Pipe()
        proc.standardError = errPipe
        proc.standardOutput = Pipe()  // discard stdout

        do {
            try proc.run()
            proc.waitUntilExit()
            if proc.terminationStatus != 0 {
                let data = errPipe.fileHandleForReading.readDataToEndOfFile()
                let msg = String(data: data, encoding: .utf8) ?? "(no stderr)"
                await MainActor.run {
                    self.lastError = "monitor refresh exit=\(proc.terminationStatus): \(msg.trimmingCharacters(in: .whitespacesAndNewlines))"
                }
            } else {
                await MainActor.run { self.lastError = nil }
            }
        } catch {
            await MainActor.run {
                self.lastError = "couldn't run monitor: \(error.localizedDescription)"
            }
        }
    }

    private func loadStateFile() async {
        let url = URL(fileURLWithPath: stateFile)
        guard let data = try? Data(contentsOf: url) else { return }
        do {
            let decoded = try JSONDecoder().decode(MonitorState.self, from: data)
            await MainActor.run { self.state = decoded }
        } catch {
            await MainActor.run {
                self.lastError = "couldn't decode state.json: \(error.localizedDescription)"
            }
        }
    }
}

// MARK: - Date/format helpers

enum Format {
    static func tokens(_ n: Int) -> String {
        if n >= 1_000_000 { return String(format: "%.1fM", Double(n) / 1_000_000) }
        if n >= 1_000     { return String(format: "%.0fk", Double(n) / 1_000) }
        return "\(n)"
    }

    static func reset(from iso: String?, now: Date = Date(), fallbackSeconds: Int? = nil) -> String {
        let secs: Int
        if let s = fallbackSeconds {
            secs = max(0, s)
        } else {
            guard let iso = iso else { return "" }
            guard let target = parseISO(iso) else { return "" }
            secs = max(0, Int(target.timeIntervalSince(now)))
        }
        if secs < 3600       { return "\(secs / 60)m" }
        if secs < 86400      { let h = secs / 3600; let m = (secs % 3600) / 60; return "\(h)h \(m)m" }
        if secs < 7 * 86400  { let days = secs / 86400; let h = (secs % 86400) / 3600; return "\(days)d \(h)h" }
        let w = secs / (7 * 86400); let days = (secs % (7 * 86400)) / 86400
        return "\(w)w \(days)d"
    }

    static func parseISO(_ iso: String) -> Date? {
        let fmt = ISO8601DateFormatter()
        fmt.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        if let d = fmt.date(from: iso) { return d }
        fmt.formatOptions = [.withInternetDateTime]
        return fmt.date(from: iso)
    }

    /// Absolute wall-clock format ("Wed 2:00 PM" / "May 27 2:00 PM") for resets
    /// that are far enough away that a relative duration is harder to read
    /// than a date. Matches Anthropic's own UI.
    static func resetAbsolute(from iso: String?) -> String {
        guard let iso = iso, let target = parseISO(iso) else { return "" }
        let cal = Calendar.current
        let daysAway = cal.dateComponents([.day], from: cal.startOfDay(for: Date()),
                                          to: cal.startOfDay(for: target)).day ?? 0
        let f = DateFormatter()
        f.locale = Locale(identifier: "en_US_POSIX")
        if daysAway < 7 {
            f.dateFormat = "EEE h:mm a"   // "Wed 2:00 PM"
        } else {
            f.dateFormat = "MMM d h:mm a" // "Jun 3 2:00 PM"
        }
        return f.string(from: target)
    }

    static func windowLabel(_ kind: String) -> String {
        switch kind {
        case "rolling_5h":         return "Current Session"
        case "rolling_7d", "weekly": return "Weekly"
        case "rolling_7d_sonnet":  return "Sonnet (weekly)"
        case "rolling_7d_opus":    return "Opus (weekly)"
        default:                   return kind
        }
    }

    /// Consistent display order across agents.
    static func windowSortKey(_ kind: String) -> Int {
        switch kind {
        case "rolling_5h":           return 0
        case "rolling_7d", "weekly": return 1
        case "rolling_7d_sonnet":    return 2
        case "rolling_7d_opus":      return 3
        default:                     return 99
        }
    }
}

// MARK: - Bundled brand icons

enum AgentIcon {
    /// Load a bundled PNG (e.g. "claude" → claude@18.png). Returns nil if missing.
    static func image(for agentId: String, size: CGFloat) -> NSImage? {
        // We bundle two retina-ish PNGs: @18 (16-20pt menu cell) and @36 (retina).
        let pick = size <= 20 ? "\(agentId)@18" : "\(agentId)@36"
        guard let url = Bundle.main.url(forResource: pick, withExtension: "png"),
              let img = NSImage(contentsOf: url) else {
            return nil
        }
        img.size = NSSize(width: size, height: size)
        // Treat as template so it auto-tints with light/dark mode.
        img.isTemplate = true
        return img
    }
}

/// Builds the menubar tray image: [claude] N% [codex] N% composited into one
/// NSImage marked as template so macOS auto-tints it.
enum TrayComposite {
    static let iconSize: CGFloat = 14
    static let textSize: CGFloat = 13
    static let iconTextGap: CGFloat = 3
    static let pairGap: CGFloat = 8
    static let height: CGFloat = 18

    static func render(claudePct: Int?, codexPct: Int?) -> NSImage {
        let font = NSFont.systemFont(ofSize: textSize, weight: .regular)
        let attrs: [NSAttributedString.Key: Any] = [
            .font: font,
            .foregroundColor: NSColor.black,  // template-image will tint
        ]

        let parts: [(NSImage?, String)] = [
            claudePct.map { ("claude", "\($0)%") },
            codexPct.map  { ("codex",  "\($0)%") },
        ]
        .compactMap { $0 }
        .map { (id, txt) in (AgentIcon.image(for: id, size: iconSize), txt) }

        guard !parts.isEmpty else {
            // Fallback: render a single em-dash glyph at menubar size
            let dash = "—"
            let w = (dash as NSString).size(withAttributes: attrs).width + 4
            return NSImage(size: NSSize(width: w, height: height), flipped: false) { _ in
                (dash as NSString).draw(at: NSPoint(x: 2, y: 0), withAttributes: attrs)
                return true
            }
        }

        // Pre-measure widths so we can size the image precisely.
        let widths: [CGFloat] = parts.map { _, txt in
            (txt as NSString).size(withAttributes: attrs).width
        }
        var width: CGFloat = 0
        for (i, (_, _)) in parts.enumerated() {
            if i > 0 { width += pairGap }
            width += iconSize + iconTextGap + widths[i]
        }

        let img = NSImage(size: NSSize(width: width, height: height), flipped: false) { _ in
            var x: CGFloat = 0
            for (i, (icon, txt)) in parts.enumerated() {
                if i > 0 { x += pairGap }
                if let icon = icon {
                    let iconY = (height - iconSize) / 2
                    icon.draw(in: NSRect(x: x, y: iconY, width: iconSize, height: iconSize))
                }
                x += iconSize + iconTextGap
                // Center text vertically inside the image height.
                let textHeight = (txt as NSString).size(withAttributes: attrs).height
                let textY = (height - textHeight) / 2
                (txt as NSString).draw(at: NSPoint(x: x, y: textY), withAttributes: attrs)
                x += widths[i]
            }
            return true
        }
        img.isTemplate = true
        return img
    }
}

struct AgentIconView: View {
    let agentId: String
    let size: CGFloat

    var body: some View {
        if let img = AgentIcon.image(for: agentId, size: size) {
            Image(nsImage: img)
                .resizable()
                .frame(width: size, height: size)
        } else {
            // Fallback: colored dot
            Circle()
                .fill(agentId == "claude" ? Color.orange : Color.teal)
                .frame(width: size * 0.4, height: size * 0.4)
        }
    }
}

// MARK: - UI components

/// Slim rounded capsule progress bar.
///
/// Neutral monochrome that adapts to theme: dark fill on light backgrounds,
/// light fill on dark. Track stays subtly visible in either. Color shifts
/// to orange/red only when usage approaches the limit so the user can't
/// miss a critical state.
struct ProgressBar: View {
    let pct: Int

    /// Fill color: theme-neutral until usage gets serious.
    private var fillColor: Color {
        if pct >= 90 { return .red }
        if pct >= 75 { return .orange }
        return Color.primary.opacity(0.75)
    }

    var body: some View {
        GeometryReader { geo in
            ZStack(alignment: .leading) {
                Capsule().fill(Color.primary.opacity(0.15))
                Capsule().fill(fillColor)
                    .frame(width: geo.size.width * CGFloat(max(0, min(100, pct))) / 100)
            }
        }
        .frame(height: 6)
    }
}

/// Extra usage credits (the "Usage credits" toggle on the Anthropic
/// dashboard). Only shown when the user opts in.
struct ExtraCreditsRow: View {
    let extra: RemoteUsage

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack(alignment: .firstTextBaseline) {
                Text("Extra credits")
                    .font(.system(size: 13, weight: .semibold))
                Spacer()
                Text("\(extra.pct)%")
                    .font(.system(size: 13, weight: .semibold))
                    .monospacedDigit()
            }
            ProgressBar(pct: extra.pct)
            HStack {
                Text("$\(extra.used) of $\(extra.limit) \(extra.ccy)")
                    .font(.system(size: 11))
                    .foregroundStyle(.secondary)
                Spacer()
            }
        }
    }
}


struct WindowRow: View {
    let window: LimitWindow

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack(alignment: .firstTextBaseline) {
                Text(Format.windowLabel(window.kind))
                    .font(.system(size: 13, weight: .semibold))
                Spacer()
                Text("\(window.pct)%")
                    .font(.system(size: 13, weight: .semibold))
                    .monospacedDigit()
                    .foregroundStyle(isExpired ? .secondary : .primary)
            }
            ProgressBar(pct: window.pct)
                .opacity(isExpired ? 0.5 : 1)
            captionView
        }
    }

    /// When `resets_at` is in the past, the recorded window has already
    /// rolled over and our cached numbers are stale (likely because the
    /// agent CLI hasn't been used since — consumption via desktop app or
    /// web isn't visible to us). Call that out instead of showing a
    /// misleading "Resets in 0m".
    private var isExpired: Bool {
        guard let secs = secondsUntilReset else { return false }
        return secs <= 0
    }

    private var secondsUntilReset: Int? {
        guard let iso = window.resetsAt else { return nil }
        let fmt = ISO8601DateFormatter()
        fmt.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        var d = fmt.date(from: iso)
        if d == nil {
            fmt.formatOptions = [.withInternetDateTime]
            d = fmt.date(from: iso)
        }
        guard let target = d else { return nil }
        return Int(target.timeIntervalSinceNow)
    }

    @ViewBuilder
    private var captionView: some View {
        if let secs = secondsUntilReset {
            if secs <= 0 {
                let expiredAgo = Format.reset(from: nil, fallbackSeconds: -secs)
                Text("Window expired \(expiredAgo) ago · data stale")
                    .font(.system(size: 11))
                    .foregroundStyle(.orange)
            } else if secs >= 86400 {
                // Weekly-ish: show absolute wall-clock ("Resets Wed 2:00 PM").
                Text("Resets \(Format.resetAbsolute(from: window.resetsAt))")
                    .font(.system(size: 11))
                    .foregroundStyle(.secondary)
            } else {
                // Within 24h: relative ("Resets in 3h 34m") is more useful.
                Text("Resets in \(Format.reset(from: window.resetsAt))")
                    .font(.system(size: 11))
                    .foregroundStyle(.secondary)
            }
        }
    }
}

struct AgentSection: View {
    let agent: AgentState

    var planLabel: String? {
        switch agent.id {
        case "claude": return "Max"
        case "codex":  return "Plus"
        default:       return nil
        }
    }

    var sectionTitle: String {
        switch agent.id {
        case "claude": return "Claude Usage"
        case "codex":  return "Codex Usage"
        default:       return "\(agent.label) Usage"
        }
    }

    var allWindows: [LimitWindow] {
        let combined = ([agent.window].compactMap { $0 }) + agent.secondaryWindows
        // Stable sort so Current Session always comes before Weekly,
        // regardless of which one is the agent's "primary" tray-facing window.
        return combined.sorted { Format.windowSortKey($0.kind) < Format.windowSortKey($1.kind) }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack(spacing: 8) {
                AgentIconView(agentId: agent.id, size: 16)
                Text(sectionTitle)
                    .font(.system(size: 14, weight: .bold))
                Spacer()
                if let plan = planLabel {
                    Text(plan)
                        .font(.system(size: 11, weight: .semibold))
                        .padding(.horizontal, 8).padding(.vertical, 2)
                        .foregroundStyle(Color.orange)
                        .background(
                            Capsule()
                                .fill(Color.orange.opacity(0.18))
                                .overlay(
                                    Capsule().strokeBorder(Color.orange.opacity(0.55), lineWidth: 1)
                                )
                        )
                }
            }
            ForEach(allWindows, id: \.kind) { w in
                WindowRow(window: w)
            }
            if let extra = agent.extraCredits {
                ExtraCreditsRow(extra: extra)
            }
            if let err = agent.errors.last {
                Text(staleMessage(err.code))
                    .font(.system(size: 10))
                    .foregroundStyle(.secondary)
            }
        }
    }

    private func staleMessage(_ code: String) -> String {
        if code.hasPrefix("stale_rate_limits_") {
            let mins = Int(code.dropFirst("stale_rate_limits_".count).dropLast()) ?? 0
            let label = mins >= 60 ? "\(mins / 60)h \(mins % 60)m" : "\(mins)m"
            return "⚠ rate limits \(label) stale (desktop / web use not seen)"
        }
        if code == "http_429" {
            return "⚠ rate-limited by Anthropic (will retry)"
        }
        if code.hasPrefix("http_") {
            return "⚠ remote fetch failed (\(code))"
        }
        if code == "no_token" {
            return "⚠ Claude token not in Keychain — sign in to claude.ai"
        }
        if code == "network" {
            return "⚠ network error — will retry"
        }
        return "⚠ \(code)"
    }
}

// MARK: - Active sessions + Projects sections

struct ActiveSection: View {
    let state: MonitorState

    var actives: [(AgentState, ThreadInfo)] {
        var out: [(AgentState, ThreadInfo)] = []
        for a in state.agents {
            for t in a.threads where (t.active || (t.pid != nil)) {
                out.append((a, t))
            }
        }
        // writing-now first, then alive, then by tokens
        return out.sorted {
            if $0.1.active != $1.1.active { return $0.1.active }
            if ($0.1.pid != nil) != ($1.1.pid != nil) { return $0.1.pid != nil }
            return $0.1.billable > $1.1.billable
        }
    }

    var body: some View {
        if !actives.isEmpty {
            VStack(alignment: .leading, spacing: 6) {
                Text("ACTIVE")
                    .font(.system(size: 10, weight: .semibold))
                    .foregroundStyle(.secondary)
                ForEach(actives, id: \.1.sid) { (agent, t) in
                    ThreadRow(agent: agent, thread: t)
                }
            }
        }
    }
}

struct ProjectsSection: View {
    let state: MonitorState

    struct ProjectGroup: Identifiable {
        let key: String
        let billable: Int
        let threads: [(AgentState, ThreadInfo)]
        var id: String { key }
        var count: Int { threads.count }
    }

    var groups: [ProjectGroup] {
        // Linear-style XYZ-123 collapses to XYZ-*
        var raw: [String: [(AgentState, ThreadInfo)]] = [:]
        for a in state.agents {
            for t in a.threads {
                let key = Self.groupKey(t.project)
                raw[key, default: []].append((a, t))
            }
        }
        return raw.map { (key, items) in
            let total = items.reduce(0) { $0 + $1.1.billable }
            return ProjectGroup(key: key, billable: total, threads: items)
        }
        .sorted { $0.billable > $1.billable }
    }

    static func groupKey(_ name: String) -> String {
        // Regex: ^[A-Z][A-Z0-9]+-\d+$
        let pattern = #"^([A-Z][A-Z0-9]+)-\d+$"#
        if let r = try? NSRegularExpression(pattern: pattern),
           let m = r.firstMatch(in: name, range: NSRange(name.startIndex..., in: name)),
           let range = Range(m.range(at: 1), in: name) {
            return String(name[range]) + "-*"
        }
        return name
    }

    var body: some View {
        if !groups.isEmpty {
            VStack(alignment: .leading, spacing: 6) {
                Text("PROJECTS")
                    .font(.system(size: 10, weight: .semibold))
                    .foregroundStyle(.secondary)
                ForEach(groups) { g in
                    ProjectRow(group: g)
                }
            }
        }
    }
}

struct ThreadRow: View {
    let agent: AgentState
    let thread: ThreadInfo

    var label: String {
        if let t = thread.title { return t }
        if let m = thread.firstMsg {
            // Strip the Linear MCP prelude
            let prelude = #"^You are working on a Linear ticket\s*`([^`]+)`\s*"#
            if let r = try? NSRegularExpression(pattern: prelude, options: .caseInsensitive) {
                let range = NSRange(m.startIndex..., in: m)
                if let match = r.firstMatch(in: m, range: range) {
                    let after = (m as NSString).replacingCharacters(in: match.range, with: "")
                    let trimmed = after.trimmingCharacters(in: .whitespacesAndNewlines)
                    if !trimmed.isEmpty { return trimmed }
                    if let tr = Range(match.range(at: 1), in: m) { return String(m[tr]) }
                }
            }
            return m
        }
        return "(untitled)"
    }

    var body: some View {
        HStack(spacing: 8) {
            AgentIconView(agentId: agent.id, size: 12)
            Text(thread.project)
                .font(.system(size: 12, weight: .medium))
                .frame(minWidth: 80, alignment: .leading)
            Text(Format.tokens(thread.billable))
                .font(.system(size: 11, design: .monospaced))
                .foregroundStyle(.secondary)
                .frame(minWidth: 50, alignment: .trailing)
            if let ctx = thread.contextPct {
                // Show REMAINING context (headroom) — what most users want to
                // see at a glance: "how much room do I have left before
                // compaction?". Cap at [0, 100] for sanity.
                let used = min(100, max(0, ctx))
                let remaining = 100 - used
                Text("ctx \(remaining)%")
                    .font(.system(size: 10, design: .monospaced))
                    .foregroundStyle(remaining <= 20 ? .orange : .secondary)
                    .help(ctxTooltip)
            }
            Text(label)
                .font(.system(size: 11))
                .foregroundStyle(.secondary)
                .lineLimit(1)
                .truncationMode(.tail)
            Spacer()
        }
    }

    private var ctxTooltip: String {
        guard let tok = thread.contextTokens, let max = thread.contextMax else {
            return ""
        }
        let free = Swift.max(0, max - tok)
        return "\(Format.tokens(tok)) used / \(Format.tokens(max)) total · \(Format.tokens(free)) free"
    }
}

struct ProjectRow: View {
    let group: ProjectsSection.ProjectGroup
    @State private var expanded = false

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            Button(action: { expanded.toggle() }) {
                HStack(spacing: 8) {
                    Image(systemName: expanded ? "chevron.down" : "chevron.right")
                        .font(.system(size: 9))
                        .foregroundStyle(.secondary)
                        .frame(width: 10)
                    Text(group.key)
                        .font(.system(size: 12, weight: .medium))
                        .frame(minWidth: 100, alignment: .leading)
                    Text(Format.tokens(group.billable))
                        .font(.system(size: 11, design: .monospaced))
                        .foregroundStyle(.secondary)
                        .frame(minWidth: 50, alignment: .trailing)
                    Text("(\(group.count))")
                        .font(.system(size: 11))
                        .foregroundStyle(.secondary)
                    Spacer()
                }
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)

            if expanded {
                VStack(alignment: .leading, spacing: 4) {
                    ForEach(group.threads.sorted(by: { $0.1.billable > $1.1.billable }), id: \.1.sid) { (a, t) in
                        HStack(spacing: 8) {
                            Spacer().frame(width: 18)
                            AgentIconView(agentId: a.id, size: 11)
                            Text(String(t.sid.prefix(8)))
                                .font(.system(size: 10, design: .monospaced))
                                .foregroundStyle(.secondary)
                            Text(Format.tokens(t.billable))
                                .font(.system(size: 10, design: .monospaced))
                                .foregroundStyle(.secondary)
                                .frame(minWidth: 50, alignment: .trailing)
                            Text(t.title ?? t.firstMsg ?? "")
                                .font(.system(size: 10))
                                .foregroundStyle(.secondary)
                                .lineLimit(1)
                                .truncationMode(.tail)
                            Spacer()
                        }
                    }
                }
                .padding(.leading, 4)
            }
        }
    }
}

struct ContentView: View {
    @ObservedObject var refresher: Refresher

    /// Cap the scrollable area so the whole popover never exceeds screen
    /// height. macOS will silently push content off-screen otherwise — the
    /// bug the user kept hitting on first-click.
    private var maxScrollHeight: CGFloat {
        let screen = NSScreen.main?.visibleFrame.height ?? 800
        // Leave room for: menubar (~24), popover arrow + padding (~16),
        // bottom margin (~40), and the FooterView (~50). The rest is content.
        return max(200, screen - 130 - 50)
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            if let state = refresher.state {
                ScrollView {
                    VStack(alignment: .leading, spacing: 18) {
                        ForEach(Array(state.agents.enumerated()), id: \.element.id) { idx, agent in
                            if idx > 0 { Divider() }
                            AgentSection(agent: agent)
                        }
                        if !ActiveSection(state: state).actives.isEmpty {
                            Divider()
                            ActiveSection(state: state)
                        }
                        if !ProjectsSection(state: state).groups.isEmpty {
                            Divider()
                            ProjectsSection(state: state)
                        }
                    }
                    .padding(16)
                    .frame(maxWidth: .infinity, alignment: .leading)
                }
                .frame(maxHeight: maxScrollHeight)
                // Hide scrollbar visually but keep scrolling — the cap is
                // usually generous enough that no scrolling is needed.
                .scrollIndicators(.never)
                .scrollBounceBehavior(.basedOnSize)
            } else {
                VStack {
                    if refresher.isRefreshing {
                        ProgressView().controlSize(.small)
                    }
                    Text("no data yet").foregroundStyle(.secondary)
                }
                .frame(height: 80)
                .frame(maxWidth: .infinity)
            }

            Divider()
            FooterView(refresher: refresher)
        }
        // Fix width, let height be intrinsic — sizingOptions on the host
        // controller reads this and sets popover.contentSize synchronously.
        .frame(width: 380, alignment: .leading)
        .fixedSize(horizontal: false, vertical: true)
    }
}

struct FooterView: View {
    @ObservedObject var refresher: Refresher

    var updatedLabel: String {
        guard let d = refresher.lastUpdated else { return "—" }
        let secs = Int(Date().timeIntervalSince(d))
        if secs < 5     { return "Updated just now" }
        if secs < 60    { return "Updated \(secs)s ago" }
        if secs < 3600  { return "Updated \(secs / 60)m ago" }
        return "Updated \(secs / 3600)h ago"
    }

    var body: some View {
        VStack(spacing: 0) {
            if let err = refresher.lastError {
                Text(err)
                    .font(.system(size: 10))
                    .foregroundStyle(.red)
                    .lineLimit(2)
                    .padding(.horizontal, 16).padding(.top, 8)
            }
            HStack(spacing: 12) {
                Text(updatedLabel)
                    .font(.system(size: 11))
                    .foregroundStyle(.secondary)
                Spacer()
                Button(action: { refresher.refresh() }) {
                    Image(systemName: "arrow.clockwise")
                }
                .buttonStyle(.borderless)
                .help("Refresh now")
                .disabled(refresher.isRefreshing)
                Button(action: openConfig) {
                    Image(systemName: "gearshape")
                }
                .buttonStyle(.borderless)
                .help("Open config")
                Button(action: { NSApp.terminate(nil) }) {
                    Image(systemName: "power")
                }
                .buttonStyle(.borderless)
                .keyboardShortcut("q")
                .help("Quit")
            }
            .padding(.horizontal, 16)
            .padding(.top, 10)
            .padding(.bottom, 14)
        }
    }

    private func openConfig() {
        let path = NSHomeDirectory() + "/.config/ai-monitor.toml"
        if FileManager.default.fileExists(atPath: path) {
            NSWorkspace.shared.open(URL(fileURLWithPath: path))
        } else {
            // Create starter via `monitor doctor --write-config` then open.
            let proc = Process()
            proc.executableURL = URL(fileURLWithPath: NSHomeDirectory() + "/Library/Python/3.13/bin/monitor")
            proc.arguments = ["doctor", "--write-config"]
            try? proc.run()
            proc.waitUntilExit()
            if FileManager.default.fileExists(atPath: path) {
                NSWorkspace.shared.open(URL(fileURLWithPath: path))
            }
        }
    }
}

// MARK: - AppDelegate

@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate {
    private var statusItem: NSStatusItem!
    private var popover: NSPopover!
    private var refresher: Refresher!
    private var eventMonitor: Any?
    private var cancellables = Set<AnyCancellable>()

    func applicationDidFinishLaunching(_ notification: Notification) {
        refresher = Refresher()

        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        statusItem.button?.action = #selector(togglePopover)
        statusItem.button?.target = self
        updateStatusTitle(state: nil)

        popover = NSPopover()
        popover.behavior = .transient
        popover.animates = false
        // Initial size — a fallback only; sizingOptions takes over once
        // SwiftUI lays out.
        popover.contentSize = NSSize(width: 380, height: 400)
        let host = NSHostingController(rootView: ContentView(refresher: refresher))
        // .intrinsicContentSize makes the host controller report the SwiftUI
        // view's natural size SYNCHRONOUSLY, so the popover is sized correctly
        // BEFORE it's shown — no first-click clipping race.
        host.sizingOptions = [.intrinsicContentSize]
        popover.contentViewController = host

        // Auto-update title when state changes
        refresher.$state
            .receive(on: DispatchQueue.main)
            .sink { [weak self] state in self?.updateStatusTitle(state: state) }
            .store(in: &cancellables)

        refresher.start()

        // Dismiss popover when clicking elsewhere
        eventMonitor = NSEvent.addGlobalMonitorForEvents(matching: [.leftMouseDown, .rightMouseDown]) { [weak self] _ in
            if self?.popover.isShown == true { self?.popover.performClose(nil) }
        }
    }

    @MainActor
    private func updateStatusTitle(state: MonitorState?) {
        guard let button = statusItem.button else { return }
        guard let state = state, !state.agents.isEmpty else {
            button.image = nil
            button.title = "—"
            return
        }
        var claudePct: Int? = nil
        var codexPct: Int? = nil
        for a in state.agents {
            // Tray always shows the *Current Session* (5h) percentage — that's
            // the "right now" signal. The weekly window lives in the dropdown.
            // For Claude the 5h is the primary `window`; for Codex it's a
            // secondary (Codex's tray-facing primary is weekly). Fall back to
            // window.pct if no 5h is present.
            let fiveHour = findFiveHourWindow(agent: a)?.pct ?? a.window?.pct
            if a.id == "claude" { claudePct = fiveHour }
            else if a.id == "codex" { codexPct = fiveHour }
        }
        button.image = TrayComposite.render(claudePct: claudePct, codexPct: codexPct)
        button.title = ""
        if claudePct != nil || codexPct != nil {
            let parts = [
                claudePct.map { "Claude (session) \($0)%" },
                codexPct.map  { "Codex (session) \($0)%" },
            ].compactMap { $0 }
            button.toolTip = parts.joined(separator: " · ")
        }
    }

    /// Find the 5h-session window for an agent, regardless of whether it sits
    /// in `window` (Claude) or `secondaryWindows` (Codex).
    private func findFiveHourWindow(agent: AgentState) -> LimitWindow? {
        if let w = agent.window, w.kind == "rolling_5h" { return w }
        for w in agent.secondaryWindows where w.kind == "rolling_5h" { return w }
        return nil
    }

    @objc private func togglePopover() {
        if popover.isShown {
            popover.performClose(nil)
        } else {
            // Force a refresh so the user sees fresh data on open.
            refresher.refresh()
            if let button = statusItem.button {
                popover.show(relativeTo: button.bounds, of: button, preferredEdge: .minY)
                popover.contentViewController?.view.window?.makeKey()
            }
        }
    }
}

// MARK: - Entrypoint

@main
struct Entrypoint {
    static func main() {
        MainActor.assumeIsolated {
            let app = NSApplication.shared
            let delegate = AppDelegate()
            app.delegate = delegate
            app.setActivationPolicy(.accessory)
            app.run()
        }
    }
}
