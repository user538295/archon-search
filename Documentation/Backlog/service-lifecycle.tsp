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
interface SearchServiceLifecycle {
  start(dryRun?: boolean): int32;
  stop(dryRun?: boolean): int32;
  status(): ServiceStatus;
  register(dryRun?: boolean): void;
  unregister(dryRun?: boolean): void;
}
