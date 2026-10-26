export type CandidateStamp = { origin?: "USER_IMPORTED_UNVERIFIED"; imported_at: string }
  | { origin: "RECORDED_ADAPTER_RESULT"; preserved_at: string }
  | { origin: "PRESERVED_REVIEW_DERIVATION"; preserved_at: string };
export type CandidateSummary = CandidateStamp & { candidate_id: string; title: string };
export const candidateTimestamp = (value: CandidateStamp) => value.origin === "RECORDED_ADAPTER_RESULT" || value.origin === "PRESERVED_REVIEW_DERIVATION" ? value.preserved_at : value.imported_at;
export const candidateOriginLabel = (value: CandidateStamp) => value.origin === "PRESERVED_REVIEW_DERIVATION" ? "저장된 분석에서 구성"
  : value.origin === "RECORDED_ADAPTER_RESULT" ? "저장된 생성 결과" : "파일로 가져온 후보";
