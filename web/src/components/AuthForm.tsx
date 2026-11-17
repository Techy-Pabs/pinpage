import { useRef, useState, type FormEvent, type ReactNode } from "react";
import { ApiError } from "../api";
import { PrimaryButton } from "./Button";
import { FormAlert } from "./FormAlert";
import { TextField } from "./TextField";

const EMAIL_PATTERN = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;
export const MIN_PASSWORD = 8;

type Fields = { email?: string; password?: string };

type AuthFormProps = {
  title: string;
  submitLabel: string;
  busyLabel: string;
  /** Register asks for a new password: the rule is shown and checked here first */
  newPassword: boolean;
  onSubmit: (email: string, password: string) => Promise<void>;
  footer: ReactNode;
};

/** The email and password form shared by sign in and register. Mistakes the
    browser can see are caught before any request; the API's errors are
    shown above the button, or under the field they belong to. */
export function AuthForm({ title, submitLabel, busyLabel, newPassword, onSubmit, footer }: AuthFormProps) {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [fieldErrors, setFieldErrors] = useState<Fields>({});
  const [formError, setFormError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const emailRef = useRef<HTMLInputElement>(null);
  const passwordRef = useRef<HTMLInputElement>(null);

  function check(): Fields {
    const errors: Fields = {};
    const trimmed = email.trim();
    if (!trimmed) errors.email = "Enter your email address.";
    else if (!EMAIL_PATTERN.test(trimmed)) errors.email = "Enter a valid email address, like name@example.com.";
    if (!password) errors.password = newPassword ? "Choose a password." : "Enter your password.";
    else if (newPassword && password.length < MIN_PASSWORD)
      errors.password = `Use at least ${MIN_PASSWORD} characters for your password.`;
    return errors;
  }

  function focusFirst(errors: Fields) {
    if (errors.email) emailRef.current?.focus();
    else if (errors.password) passwordRef.current?.focus();
  }

  async function handleSubmit(event: FormEvent) {
    event.preventDefault();
    if (busy) return;
    const errors = check();
    setFieldErrors(errors);
    setFormError(null);
    if (errors.email || errors.password) {
      focusFirst(errors);
      return;
    }

    setBusy(true);
    try {
      await onSubmit(email.trim(), password);
    } catch (error) {
      setBusy(false);
      if (error instanceof ApiError && (error.fields.email || error.fields.password)) {
        const fromApi = { email: error.fields.email, password: error.fields.password };
        setFieldErrors(fromApi);
        focusFirst(fromApi);
      } else {
        setFormError(error instanceof ApiError ? error.message : "Something went wrong. Try again.");
      }
    }
  }

  return (
    <div className="flex flex-col gap-8">
      <h1 className="text-page-title text-ink">{title}</h1>
      <form noValidate onSubmit={handleSubmit} className="flex flex-col gap-5">
        <TextField
          ref={emailRef}
          label="Email"
          name="email"
          type="email"
          autoComplete={newPassword ? "email" : "username"}
          inputMode="email"
          autoCapitalize="none"
          spellCheck={false}
          value={email}
          onChange={(event) => setEmail(event.target.value)}
          error={fieldErrors.email}
        />
        <TextField
          ref={passwordRef}
          label="Password"
          name="password"
          type="password"
          autoComplete={newPassword ? "new-password" : "current-password"}
          value={password}
          onChange={(event) => setPassword(event.target.value)}
          error={fieldErrors.password}
          hint={newPassword ? `At least ${MIN_PASSWORD} characters.` : undefined}
        />
        <FormAlert message={formError} />
        <PrimaryButton busy={busy} className="mt-1">
          {busy ? busyLabel : submitLabel}
        </PrimaryButton>
      </form>
      <p className="text-body text-ink-muted">{footer}</p>
    </div>
  );
}
