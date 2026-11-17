import type { ReactNode } from "react";
import { ExampleSheet } from "./ExampleSheet";
import { Wordmark } from "./Wordmark";

/** Sign in and register: the green cover beside a white form. Stacks on a
    phone, cover first, with the example sheet left out to keep the form near. */
export function AuthLayout({ children }: { children: ReactNode }) {
  return (
    <div className="grid min-h-dvh bg-sheet md:grid-cols-[minmax(0,7fr)_minmax(0,5fr)]">
      <section className="on-cover flex flex-col gap-8 bg-cover px-5 py-6 text-cover-ink md:px-12 md:py-10 lg:px-16">
        <Wordmark />
        <div className="flex flex-1 flex-col justify-center gap-10">
          <p className="max-w-[22ch] text-page-title text-cover-ink">
            Ask your handbook a question. Check the answer on its page.
          </p>
          <div className="hidden md:block">
            <ExampleSheet />
          </div>
        </div>
        <p className="text-small text-cover-muted">A portfolio demo. Don't upload anything confidential.</p>
      </section>

      <main className="flex items-start justify-center px-5 py-10 md:items-center md:px-12">
        <div className="w-full max-w-[22.5rem]">{children}</div>
      </main>
    </div>
  );
}
