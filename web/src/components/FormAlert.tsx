import { AlertIcon } from "./TextField";

/** The form's error, read out as soon as it appears. */
export function FormAlert({ message }: { message: string | null }) {
  return (
    <div role="alert" aria-live="assertive" className="empty:hidden">
      {message && (
        <p className="flex items-start gap-2 rounded-sheet bg-examiner-tint px-3 py-2.5 text-body text-ink">
          <span className="text-examiner">
            <AlertIcon />
          </span>
          <span>{message}</span>
        </p>
      )}
    </div>
  );
}
