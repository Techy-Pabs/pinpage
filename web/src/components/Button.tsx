import type { ButtonHTMLAttributes } from "react";

type ButtonProps = ButtonHTMLAttributes<HTMLButtonElement> & { busy?: boolean };

/** The primary button: solid ballpoint, 44 px. While busy it is disabled
    and says so. */
export function PrimaryButton({ busy = false, disabled, className = "", children, ...button }: ButtonProps) {
  return (
    <button
      type="submit"
      disabled={disabled || busy}
      aria-busy={busy || undefined}
      className={`inline-flex h-11 w-full items-center justify-center rounded-sheet bg-ballpoint px-5 text-body font-semibold text-sheet transition-[background-color,opacity] duration-150 hover:bg-ballpoint/90 disabled:cursor-not-allowed disabled:opacity-40 disabled:hover:bg-ballpoint ${className}`}
      {...button}
    >
      {children}
    </button>
  );
}
