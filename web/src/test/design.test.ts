import { readdirSync, readFileSync, statSync } from "node:fs";
import { join, relative } from "node:path";
import { describe, expect, it } from "vitest";

const SRC = join(__dirname, "..");
const THEME = join(SRC, "theme.css");
const DESIGN = join(SRC, "..", "..", "docs", "design", "DESIGN.md");

function sourceFiles(dir: string): string[] {
  return readdirSync(dir).flatMap((name) => {
    const path = join(dir, name);
    if (statSync(path).isDirectory()) return sourceFiles(path);
    return /\.(tsx?|css)$/.test(name) && !/\.test\.tsx?$/.test(name) ? [path] : [];
  });
}

// A raw colour: #abc, #aabbcc, #aabbccdd, or rgb()/hsl()/oklch() written out
const RAW_COLOUR = /#[0-9a-f]{3,8}\b|\b(?:rgba?|hsla?|oklch|oklab|lab|lch)\(/gi;

describe("the design tokens", () => {
  it("are the only colours: no component uses a raw colour value", () => {
    const offenders = sourceFiles(SRC)
      .filter((path) => path !== THEME)
      .flatMap((path) =>
        readFileSync(path, "utf8")
          .split("\n")
          .flatMap((line, index) =>
            (line.match(RAW_COLOUR) ?? []).map((found) => `${relative(SRC, path)}:${index + 1} ${found}`),
          ),
      );

    expect(offenders, "use a colour token from theme.css instead").toEqual([]);
  });

  it("match DESIGN.md exactly, every colour by name", () => {
    const design = readFileSync(DESIGN, "utf8");
    const expected = Object.fromEntries(
      [...design.matchAll(/^\| ([a-z-]+) \| (#[0-9A-F]{6}) \|/gm)].map(([, name, hex]) => [name, hex.toUpperCase()]),
    );
    const theme = readFileSync(THEME, "utf8");
    const actual = Object.fromEntries(
      [...theme.matchAll(/--color-([a-z-]+): (#[0-9A-Fa-f]{6});/g)].map(([, name, hex]) => [name, hex.toUpperCase()]),
    );

    expect(Object.keys(expected)).toEqual([
      "cover", "cover-ink", "cover-muted", "desk", "sheet", "ink", "ink-muted", "rule", "field",
      "margin-rule", "ballpoint", "ballpoint-tint", "examiner", "examiner-tint", "waiting",
    ]);
    expect(actual).toEqual(expected);
  });

  it("catch a component that slips in a raw colour", () => {
    const sample = 'className="bg-[#0E5446] text-ink" style={{ color: "rgb(0 0 0)" }}';
    expect(sample.match(RAW_COLOUR)).toEqual(["#0E5446", "rgb("]);
  });
});
