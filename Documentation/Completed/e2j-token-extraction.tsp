namespace TokenExtractionSeam;

/** /view accepts a Bearer token via two paths:
    (1) Authorization: Bearer <token> header — middleware validates before handler runs;
        handler re-reads the raw token from the header value.
    (2) ?token=<raw> query parameter — /view is exempt from the middleware's
        header-presence check when this param is present; the route handler
        validates inline via the same SHA-256 key lookup and exits 401 on failure.
    In both cases the validated raw token string is substituted into the HTML
    template via the C4 placeholder __ARCHON_TOKEN__. */
model ExtractedToken {
  /** Raw token string, without the "Bearer " prefix. Already validated
      before the happy path proceeds. */
  raw_token: string;
  /** Namespace resolved by the middleware (or by inline lookup for the query-param path). */
  ns: string;
}

interface TokenExtractor {
  /** Resolve and validate the Bearer token from whichever source is present.
      Priority: Authorization header over ?token= query param. The ?token= path
      is active only when the Authorization header is absent (middleware exemption
      predicate: path matches ^/graph/[^/]+/view$ AND "token" in query_params
      AND "authorization" not in request.headers.keys()).
      Returns null if neither source supplies a valid token (caller returns 401). */
  extract(authorization_header: string | null, token_query_param: string | null): ExtractedToken | null;
}
