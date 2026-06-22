namespace ArchonSearch.D5;

/** Response body for POST /maintenance/trigger (202 Accepted).
 *  The pass runs asynchronously; observe progress via GET /status. */
model MaintenanceTriggerResponse {
  status: "triggered" | "already_triggered";
}
