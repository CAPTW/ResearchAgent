export const SAVED_PAPERS_ROUTE_ALIAS = "saved";
export const SAVED_PAPERS_COLLECTION_ID = "saved-papers";

export type PaperCollectionRoute =
  | { kind: "all"; routeValue: ""; collectionId: "" }
  | { kind: "saved"; routeValue: typeof SAVED_PAPERS_ROUTE_ALIAS; collectionId: typeof SAVED_PAPERS_COLLECTION_ID }
  | { kind: "named"; routeValue: string; collectionId: string }
  | { kind: "malformed"; routeValue: string; collectionId: "" };

export type PaperWorkspaceRouteState = {
  query: string;
  provider: string;
  collection: PaperCollectionRoute;
  reviewState: string;
  year: string;
  openAccess: string;
  sort: string;
  page: number;
  openPaperVersionId: string;
  openEvidenceId: string;
};

export type PapersRefreshState = {
  supported: true;
  phase: "idle" | "refreshing" | "succeeded" | "failed";
  generation: number;
  failureReason: string | null;
  lastSuccessfulAt: string | null;
};

export function createPapersRefreshState(): PapersRefreshState {
  return {
    supported: true,
    phase: "idle",
    generation: 0,
    failureReason: null,
    lastSuccessfulAt: null,
  };
}

const COLLECTION_ID_PATTERN = /^[a-z0-9](?:[a-z0-9-]{0,126}[a-z0-9])?$/;

export function parsePaperCollectionRoute(value: string | null): PaperCollectionRoute {
  if (value === null || value === "") {
    return { kind: "all", routeValue: "", collectionId: "" };
  }
  if (value === SAVED_PAPERS_ROUTE_ALIAS || value === SAVED_PAPERS_COLLECTION_ID) {
    return {
      kind: "saved",
      routeValue: SAVED_PAPERS_ROUTE_ALIAS,
      collectionId: SAVED_PAPERS_COLLECTION_ID,
    };
  }
  if (!COLLECTION_ID_PATTERN.test(value)) {
    return { kind: "malformed", routeValue: value, collectionId: "" };
  }
  return { kind: "named", routeValue: value, collectionId: value };
}

export function parsePaperWorkspaceRoute(searchParams: URLSearchParams): PaperWorkspaceRouteState {
  return {
    query: searchParams.get("query") ?? "",
    provider: searchParams.get("provider") ?? "",
    collection: parsePaperCollectionRoute(searchParams.get("collection")),
    reviewState: searchParams.get("review") ?? "",
    year: searchParams.get("year") ?? "",
    openAccess: searchParams.get("openAccess") ?? searchParams.get("access") ?? "",
    sort: searchParams.get("sort") ?? "published_desc",
    page: Math.max(1, Number.parseInt(searchParams.get("page") ?? "1", 10) || 1),
    openPaperVersionId: searchParams.get("paper") ?? "",
    openEvidenceId: searchParams.get("evidence") ?? "",
  };
}

export function collectionIdToRouteValue(collectionId: string): string {
  return collectionId === SAVED_PAPERS_COLLECTION_ID ? SAVED_PAPERS_ROUTE_ALIAS : collectionId;
}

export function paperCollectionLabel(
  route: PaperCollectionRoute,
  collections: ReadonlyArray<{ id: string; name: string }> | null | undefined,
): string {
  if (route.kind === "all") return "전체";
  if (route.kind === "saved") return collections?.find((item) => item.id === SAVED_PAPERS_COLLECTION_ID)?.name ?? "Saved papers";
  if (route.kind === "malformed") return "잘못된 컬렉션 경로";
  return collections?.find((item) => item.id === route.collectionId)?.name ?? route.collectionId;
}
