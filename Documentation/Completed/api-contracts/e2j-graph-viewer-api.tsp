import "@typespec/http";

using Http;

@service(#{ title: "Graph Viewer API — E2j: single-file interactive graph viewer endpoint" })
namespace GraphViewerApi;

/** Successful HTML response: a self-contained page with the viewer. */
model GraphViewerHtmlResponse {
  @statusCode statusCode: 200;
  @header("Content-Type") contentType: "text/html";
  /** Self-contained HTML document; Bearer token and collection name are embedded
      as JS variables so the page can fetch /graph/{collection} without a second
      login prompt. Zero external network requests — all assets inlined. */
  @body body: bytes;
}

/** Graph feature is disabled (graph.enabled = false in config). */
model GraphDisabledResponse {
  @statusCode statusCode: 422;
  detail: string;
}

@route("/graph/{collection}/view")
interface GraphViewer {
  /** Render the interactive graph viewer for a collection.
      Auth: Authorization: Bearer <token> header (standard middleware path, takes priority)
      OR ?token=<raw> query parameter (middleware exemption — active only when no
      Authorization header is present; handler validates via validate_token_and_get_namespace).
      The resolved token is injected into the returned HTML so subsequent JS fetches
      are authenticated automatically. */
  @get view(@path collection: string, @query token?: string): GraphViewerHtmlResponse | UnauthorizedResponse | NotFoundResponse | GraphDisabledResponse;
}
