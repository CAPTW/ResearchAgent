const DEFAULT_APP_BASE_PATH = "/app";
const MAX_RETURN_TARGET_CHARACTERS = 2048;
const MAX_PERCENT_DECODE_PASSES = 3;
const CONTROL_CHARACTER = /[\u0000-\u001f\u007f-\u009f]/;
const ENCODED_PATH_SEPARATOR = /%(?:2f|5c)/i;
const EXTERNAL_SCHEME = /^[a-z][a-z0-9+.-]*:/i;

export type BrowserLocationParts = Pick<Location, "pathname" | "search" | "hash">;

export function normalizeAppBasePath(value: string | undefined): string {
  const candidate = (value ?? DEFAULT_APP_BASE_PATH).trim() || DEFAULT_APP_BASE_PATH;
  const withLeadingSlash = candidate.startsWith("/") ? candidate : `/${candidate}`;
  const normalized = withLeadingSlash.replace(/\/+$/, "") || "/";
  if (
    normalized.startsWith("//") ||
    normalized.includes("\\") ||
    CONTROL_CHARACTER.test(normalized) ||
    normalized.includes("?") ||
    normalized.includes("#")
  ) {
    return DEFAULT_APP_BASE_PATH;
  }
  return normalized;
}

function pathMatchesBase(pathname: string, basePath: string): boolean {
  if (basePath === "/") {
    return pathname.startsWith("/") && !pathname.startsWith("//");
  }
  return pathname === basePath || pathname.startsWith(`${basePath}/`);
}

function queryContainsExternalTarget(searchParams: URLSearchParams): boolean {
  for (const value of searchParams.values()) {
    const candidate = value.trim();
    if (!candidate) continue;
    if (
      candidate.startsWith("//") ||
      candidate.includes("\\") ||
      CONTROL_CHARACTER.test(candidate) ||
      EXTERNAL_SCHEME.test(candidate)
    ) {
      return true;
    }
  }
  return false;
}

function validateRepresentation(value: string, basePath: string): boolean {
  if (
    !value.startsWith("/") ||
    value.startsWith("//") ||
    value.includes("\\") ||
    CONTROL_CHARACTER.test(value) ||
    ENCODED_PATH_SEPARATOR.test(value)
  ) {
    return false;
  }
  try {
    const parsed = new URL(value, "https://xrml.invalid");
    return (
      parsed.origin === "https://xrml.invalid" &&
      pathMatchesBase(parsed.pathname, basePath) &&
      !queryContainsExternalTarget(parsed.searchParams)
    );
  } catch {
    return false;
  }
}

export function sanitizeFrontendReturnTarget(
  value: string | null | undefined,
  configuredBasePath: string | undefined = import.meta.env.BASE_URL,
): string {
  const basePath = normalizeAppBasePath(configuredBasePath);
  if (!value || value.length > MAX_RETURN_TARGET_CHARACTERS) {
    return basePath;
  }

  let current = value;
  for (let pass = 0; pass < MAX_PERCENT_DECODE_PASSES; pass += 1) {
    if (!validateRepresentation(current, basePath)) {
      return basePath;
    }
    let decoded: string;
    try {
      decoded = decodeURIComponent(current);
    } catch {
      return basePath;
    }
    if (decoded === current) {
      const parsed = new URL(value, "https://xrml.invalid");
      return `${parsed.pathname}${parsed.search}${parsed.hash}`;
    }
    current = decoded;
  }

  if (!validateRepresentation(current, basePath)) {
    return basePath;
  }
  try {
    if (decodeURIComponent(current) !== current) {
      return basePath;
    }
    const parsed = new URL(value, "https://xrml.invalid");
    return `${parsed.pathname}${parsed.search}${parsed.hash}`;
  } catch {
    return basePath;
  }
}

export function currentAppReturnTarget(
  location: BrowserLocationParts,
  configuredBasePath: string | undefined = import.meta.env.BASE_URL,
): string {
  return sanitizeFrontendReturnTarget(
    `${location.pathname || ""}${location.search || ""}${location.hash || ""}`,
    configuredBasePath,
  );
}

export function buildLoginNavigationUrl(loginUrl: string, returnTarget: string, configuredBasePath?: string): string {
  const safeReturnTarget = sanitizeFrontendReturnTarget(returnTarget, configuredBasePath);
  const separator = loginUrl.includes("?") ? "&" : "?";
  return `${loginUrl}${separator}return_to=${encodeURIComponent(safeReturnTarget)}`;
}
