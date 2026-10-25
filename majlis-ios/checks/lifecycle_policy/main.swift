// Checks the pause/resume/kill decisions in Majlis/Party/LifecyclePolicy.swift
// without Xcode or a simulator:
//   swiftc majlis-ios/Majlis/Party/LifecyclePolicy.swift majlis-ios/checks/lifecycle_policy/main.swift -o /tmp/lifecycle_policy && /tmp/lifecycle_policy
import Foundation

var failures = 0
func check(_ ok: Bool, _ what: String) {
    print(ok ? "ok   " : "FAIL ", what)
    if !ok { failures += 1 }
}

// No room hosted: nothing to checkpoint, nothing to resume.
check(LifecyclePolicy.onBackground(hostedRoomCode: nil, revision: 3, now: 100) == nil,
      "backgrounding with no hosted room makes no checkpoint")
check(LifecyclePolicy.onForeground(nil, now: 100) == .none, "foregrounding with no checkpoint does nothing")
check(LifecyclePolicy.onColdLaunch(leftoverCheckpoint: nil) == .none, "cold launch with no leftover checkpoint does nothing")

// Normal pause/resume: same room, within the window, rebinds.
let cp = LifecyclePolicy.onBackground(hostedRoomCode: "ABCD2345", revision: 7, now: 1000)
check(cp == LifecyclePolicy.Checkpoint(roomCode: "ABCD2345", revision: 7, pausedAt: 1000), "checkpoint captures room, revision and pause time")
check(LifecyclePolicy.onForeground(cp, now: 1030) == .rebind(cp!), "returning 30s later rebinds the same room")
check(LifecyclePolicy.onForeground(cp, now: 1000 + LifecyclePolicy.maxPauseInterval - 1) == .rebind(cp!),
      "returning just under the pause window still rebinds")

// Too long away, or a clock that went backwards: end the old session rather
// than silently resuming (or reopening) under the old identity.
check(LifecyclePolicy.onForeground(cp, now: 1000 + LifecyclePolicy.maxPauseInterval + 1) == .endExpired(cp!),
      "returning after the pause window ends the old session instead of resuming")
check(LifecyclePolicy.onForeground(cp, now: 999) == .endExpired(cp!),
      "a now() before the pause time (clock went backwards) ends the old session, never rebinds")

// Process kill, or a cold launch finding a leftover checkpoint: memory-only
// state cannot survive the process, so this is always an end, never a
// reopened room presented as the old one.
check(LifecyclePolicy.onColdLaunch(leftoverCheckpoint: cp) == .endExpired(cp!),
      "a leftover checkpoint from before the process died is always ended, never silently resumed as a new room")

// Checkpoint plausibility: a persisted value is untrusted input, not just a
// typed struct. Room codes are party/core's CODE_RE (^[A-Z2-9]{6}$, no 0/1).
// Note: `cp`'s room code ("ABCD2345") is 8 chars, used only above to exercise
// the pause/resume/expiry window logic — isPlausible is checked separately
// against party/core's real 6-char CODE_RE shape.
check(LifecyclePolicy.isPlausible(LifecyclePolicy.Checkpoint(roomCode: "ABCD23", revision: 7, pausedAt: 1000)),
      "a well-formed 6-char code using only A-Z/2-9 is plausible")
check(!LifecyclePolicy.isPlausible(LifecyclePolicy.Checkpoint(roomCode: "ABCD01", revision: 7, pausedAt: 1000)),
      "a code containing 0 or 1 (outside party/core's CODE_RE) is implausible")
check(!LifecyclePolicy.isPlausible(LifecyclePolicy.Checkpoint(roomCode: "ABCDEFG", revision: 7, pausedAt: 1000)),
      "a code of the wrong length is implausible")
check(!LifecyclePolicy.isPlausible(LifecyclePolicy.Checkpoint(roomCode: "ABCD23", revision: 0, pausedAt: 1000)),
      "a revision below 1 (rooms start at revision 1) is implausible")
check(!LifecyclePolicy.isPlausible(LifecyclePolicy.Checkpoint(roomCode: "ABCD23", revision: 7, pausedAt: -1)),
      "a negative pause time is implausible")
check(!LifecyclePolicy.isPlausible(LifecyclePolicy.Checkpoint(roomCode: "ABCD23", revision: 7, pausedAt: .infinity)),
      "a non-finite pause time is implausible")

// HostMarker plausibility (blocker 4): same room-code/revision shape as
// Checkpoint, no pausedAt to validate.
check(LifecyclePolicy.isPlausible(LifecyclePolicy.HostMarker(roomCode: "ABCD23", revision: 1, state: .active)),
      "a well-formed active host marker is plausible")
check(LifecyclePolicy.isPlausible(LifecyclePolicy.HostMarker(roomCode: "ABCD23", revision: 4, state: .paused)),
      "a well-formed paused host marker is plausible")
check(!LifecyclePolicy.isPlausible(LifecyclePolicy.HostMarker(roomCode: "ABCD01", revision: 1, state: .active)),
      "a host marker with an invalid room code is implausible")
check(!LifecyclePolicy.isPlausible(LifecyclePolicy.HostMarker(roomCode: "ABCD23", revision: 0, state: .active)),
      "a host marker with revision below 1 is implausible")

print(failures == 0 ? "all lifecycle policy checks passed" : "\(failures) lifecycle policy check(s) failed")
exit(failures == 0 ? 0 : 1)
