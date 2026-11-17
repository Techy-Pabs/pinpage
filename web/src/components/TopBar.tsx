import { Link, useNavigate } from "react-router";
import { HOME, signOut } from "../api";
import { Wordmark } from "./Wordmark";

/** The 56 px cover bar: the wordmark (to the library) and sign out. */
export function TopBar() {
  const navigate = useNavigate();

  return (
    <header className="on-cover flex h-14 items-center justify-between bg-cover px-4 text-cover-ink md:px-5">
      <Link to={HOME} className="flex h-11 items-center rounded-sheet px-1 text-cover-ink no-underline">
        <Wordmark />
        <span className="sr-only">, your library</span>
      </Link>
      <button
        type="button"
        onClick={() => {
          signOut();
          navigate("/sign-in", { replace: true });
        }}
        className="h-11 rounded-sheet px-3 text-body font-medium text-cover-ink underline decoration-cover-muted underline-offset-[3px] hover:decoration-cover-ink"
      >
        Sign out
      </button>
    </header>
  );
}
