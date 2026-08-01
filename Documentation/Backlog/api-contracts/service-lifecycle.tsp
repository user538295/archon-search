namespace ServiceLifecycleContract;

// Entity crossing the CLI <-> platform-service seam.
model ServiceStatus {
  running: boolean;
  pid: int32 | null;
  uptimeSeconds: float64 | null;
}

// The internal logical seam between CLI commands and the platform
// service implementation (SystemdSearchService / Launchd / Windows).
//
// Contract change: status() must NEVER raise when the service manager
// binary (systemctl) is absent or unreachable — it returns
// ServiceStatus(running=false, pid=null, uptimeSeconds=null) with a
// logged warning. start/stop/register/unregister are meaningless in
// container mode and surface a clean, typed failure instead of a
// raw traceback.
//
// Contract change (non-blocking-stop feature): stop() no longer blocks
// on _wait_until_stopped(). It issues the OS stop command and returns
// immediately. Return semantics change accordingly:
//   0 = stop command ISSUED (was: process confirmed DOWN after polling)
// The old "confirmed down" guarantee is gone — callers that need it must
// now poll GET /health externally (503 while stopping, then connection
// refused once stopped). This supersedes the prior blocking semantics.
interface SearchServiceLifecycle {
  start(dryRun?: boolean): int32;
  stop(dryRun?: boolean): int32;
  status(): ServiceStatus;
  register(dryRun?: boolean): void;
  unregister(dryRun?: boolean): void;
}
