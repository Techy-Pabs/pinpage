import type { DocumentStage, PinpageDocument } from "../api";
import { TickIcon } from "./icons";
import { AlertIcon } from "./TextField";

/** What a processing document is doing, in plain words (GET /documents' stage). */
export const STAGE_LABELS: Record<DocumentStage, string> = {
  extracting: "Reading pages",
  finding_sections: "Finding sections",
  embedding: "Preparing for questions",
};

export function stageLabel(stage: DocumentStage | null): string {
  return (stage && STAGE_LABELS[stage]) || "Processing";
}

/** The waiting state: its words in the waiting colour over a 2 px moving line. */
export function Waiting({ label }: { label: string }) {
  return (
    <span className="flex w-40 flex-col gap-1.5">
      <span className="text-body font-medium text-waiting">{label}</span>
      <span aria-hidden="true" className="relative block h-0.5 w-full overflow-hidden bg-rule">
        <span className="absolute inset-y-0 left-0 w-1/3 bg-waiting motion-safe:animate-waiting" />
      </span>
    </span>
  );
}

/** Ready (a tick), processing (its stage), or failed (the API's reason). */
export function StatusCell({ document }: { document: PinpageDocument }) {
  if (document.status === "ready") {
    return (
      <span className="flex items-center gap-1.5 text-body text-ink">
        <span className="text-cover">
          <TickIcon />
        </span>
        Ready
      </span>
    );
  }
  if (document.status === "processing") {
    return <Waiting label={stageLabel(document.stage)} />;
  }
  return (
    <span className="flex max-w-[18rem] flex-col gap-0.5">
      <span className="flex items-center gap-1.5 text-body font-medium text-examiner">
        <AlertIcon />
        Failed
      </span>
      <span className="text-small text-examiner">
        {document.error_message ?? "Pinpage couldn't prepare this document."}
      </span>
    </span>
  );
}
