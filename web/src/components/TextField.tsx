import { forwardRef, useId, type InputHTMLAttributes } from "react";

type TextFieldProps = InputHTMLAttributes<HTMLInputElement> & {
  label: string;
  hint?: string;
  error?: string;
};

/** A labelled 44 px input. Its hint and error are tied to it for screen
    readers; an error also marks the field invalid. */
export const TextField = forwardRef<HTMLInputElement, TextFieldProps>(function TextField(
  { label, hint, error, id, className = "", ...input },
  ref,
) {
  const generated = useId();
  const fieldId = id ?? generated;
  // The hint gives way to the error, which says the same thing more specifically
  const hintId = hint && !error ? `${fieldId}-hint` : undefined;
  const errorId = error ? `${fieldId}-error` : undefined;
  const describedBy = [errorId, hintId].filter(Boolean).join(" ") || undefined;

  return (
    <div className={`flex flex-col gap-1.5 ${className}`}>
      <label htmlFor={fieldId} className="text-body font-medium text-ink">
        {label}
      </label>
      <input
        ref={ref}
        id={fieldId}
        aria-invalid={error ? true : undefined}
        aria-describedby={describedBy}
        className={`h-11 w-full rounded-sheet border bg-sheet px-3 text-field text-ink transition-colors duration-150 hover:border-ink ${
          error ? "border-examiner" : "border-field"
        }`}
        {...input}
      />
      {error && (
        <p id={errorId} className="flex items-start gap-1.5 text-small text-examiner">
          <AlertIcon />
          <span>{error}</span>
        </p>
      )}
      {hint && !error && (
        <p id={hintId} className="text-small text-ink-muted">
          {hint}
        </p>
      )}
    </div>
  );
});

export function AlertIcon() {
  return (
    <svg aria-hidden="true" viewBox="0 0 16 16" className="mt-px size-4 shrink-0" fill="none">
      <circle cx="8" cy="8" r="6.25" stroke="currentColor" strokeWidth="1.5" />
      <path d="M8 4.75v3.75" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
      <circle cx="8" cy="11" r="0.9" fill="currentColor" />
    </svg>
  );
}
