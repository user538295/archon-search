namespace ViewerTemplateData;

/** Data the route handler substitutes into the HTML template at render time.
    Placeholder tokens are replaced with per-type escaping: string values use
    json.dumps() (adds quotes and escapes special chars); integer values use str(int()).
    Bare placeholders in the JS (no surrounding quotes) prevent double-quoting when
    json.dumps() adds quotes at substitution time. */
model TemplateInjection {
  /** Collection name for the JS fetch URL — e.g. "my-docs". json.dumps()-escaped. */
  collection: string;
  /** Validated Bearer token string for JS API calls. json.dumps()-escaped into a
      JS string literal. Acceptable for local/private deployments only. */
  token: string;
  /** config.graph.max_inspection_nodes — display-only: shown in the truncation banner
      as the "Y" cap (not forwarded to GET /graph/{collection}; the server enforces
      truncation independently). Banner counts come from the API response (nodes.length
      and data.node_count), not these injected values. */
  max_nodes: int32;
  /** config.graph.max_inspection_edges — display-only, same rationale as max_nodes. */
  max_edges: int32;
}

interface HtmlRenderer {
  /** Produce a rendered HTML string by substituting TemplateInjection values
      into the placeholder-bearing template bytes loaded from the package resource. */
  render(template_bytes: bytes, data: TemplateInjection): string;
}
