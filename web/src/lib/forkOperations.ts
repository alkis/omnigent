// Cross-module coordination for async fork operations.
//
// Fork materialization is a background server operation: the POST returns an
// `operation_id`, and a `fork_status` event on the session-updates stream later
// reports `ready` (with the destination `fork_id` AND the echoed runner-bind
// intent) or `failed`. The always-mounted SessionUpdatesProvider handles those
// events. Two small pieces of state bridge the fork dialog (which unmounts on
// close) and that provider:
//
//   1. The ORIGINAL fork request, stashed per operation id, so the failure
//      toast's "Try again" reopens the dialog with the SAME parameters
//      (notably `up_to_response_id` — otherwise a "fork from here" retry would
//      silently become a full-history fork).
//   2. A REOPEN opener so that retry can bring the dialog back for the source.
//
// The coding-fork runner bind is NOT held here anymore: the server echoes the
// bind intent on the `ready` event, so the provider binds from the event and
// the intent survives a refresh (it never lived only in this tab's memory).

/** The original fork request for a source, replayed verbatim on retry. */
export interface ForkRetryRequest {
  sourceId: string;
  upToResponseId?: string;
}

const retryRequests = new Map<string, ForkRetryRequest>();

/** Stash the original request so a failed op can be retried with the same params. */
export function registerForkRetryRequest(operationId: string, request: ForkRetryRequest): void {
  retryRequests.set(operationId, request);
}

/** Pop the stashed request for an operation (undefined if never registered). */
export function takeForkRetryRequest(operationId: string): ForkRetryRequest | undefined {
  const request = retryRequests.get(operationId);
  retryRequests.delete(operationId);
  return request;
}

type ReopenForkDialog = (request: ForkRetryRequest) => void;

let reopenForkDialog: ReopenForkDialog | null = null;

/** AppShell registers its dialog opener so a failure toast can reopen the fork. */
export function setForkDialogReopener(reopen: ReopenForkDialog | null): void {
  reopenForkDialog = reopen;
}

/** Reopen the fork dialog for a source with its original params; no-op if none. */
export function reopenForkDialogForRetry(request: ForkRetryRequest): void {
  reopenForkDialog?.(request);
}
