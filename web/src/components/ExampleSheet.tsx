/** The cover's example answer: a question in ballpoint, one answer sentence,
    and its examiner's mark in the ruled margin. Decoration only, so hidden
    from screen readers; the cover's line says the same in words. */
export function ExampleSheet() {
  return (
    <figure aria-hidden="true" className="m-0 w-full max-w-[30rem]">
      <div className="rounded-sheet bg-sheet py-6 pr-6 shadow-sheet">
        <div className="grid grid-cols-[88px_1fr] gap-x-6">
          <div className="border-r border-margin-rule" />
          <p className="text-question text-ballpoint">How many days of annual leave do new staff get?</p>

          <div className="border-r border-margin-rule pr-2.5 pt-5 text-right">
            <span className="block text-mark text-examiner">p. 10</span>
            <span className="block text-mark-section text-ink-muted tabular">4.1</span>
          </div>
          <p className="max-w-answer pt-5 text-answer text-ink">
            Full time staff get 20 working days of paid annual leave in each leave year, in addition to public
            holidays.
          </p>
        </div>
      </div>
      <figcaption className="mt-3 text-small text-pretty text-cover-muted">
        An example answer. The mark in the margin is the page and section it came from.
      </figcaption>
    </figure>
  );
}
