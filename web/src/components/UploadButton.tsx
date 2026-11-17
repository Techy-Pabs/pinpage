import { forwardRef, useRef } from "react";
import { UploadIcon } from "./icons";

type UploadButtonProps = { onFile: (file: File) => void };

/** "Upload a PDF": a real button that opens the browser's file picker. The
    file input itself stays out of sight and out of the tab order. */
export const UploadButton = forwardRef<HTMLButtonElement, UploadButtonProps>(function UploadButton({ onFile }, ref) {
  const input = useRef<HTMLInputElement>(null);

  return (
    <>
      <button
        ref={ref}
        type="button"
        onClick={() => input.current?.click()}
        className="inline-flex h-11 shrink-0 items-center justify-center gap-2 rounded-sheet bg-ballpoint px-5 text-body font-semibold text-sheet transition-colors duration-150 hover:bg-ballpoint/90"
      >
        <UploadIcon />
        Upload a PDF
      </button>
      <input
        ref={input}
        type="file"
        accept="application/pdf,.pdf"
        tabIndex={-1}
        aria-hidden="true"
        className="sr-only"
        data-testid="file-input"
        onChange={(event) => {
          const file = event.target.files?.[0];
          event.target.value = ""; // the same file can be chosen again
          if (file) onFile(file);
        }}
      />
    </>
  );
});
