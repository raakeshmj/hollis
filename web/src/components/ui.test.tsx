import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { Arguments, Phase } from "./ui";

describe("Phase", () => {
  it("pairs every phase's color with a glyph and a word", () => {
    const { container } = render(<Phase phase="waiting_approval" />);
    expect(screen.getByText("waiting approval")).toBeInTheDocument();
    expect(container.querySelector("svg")).toHaveAttribute("aria-hidden", "true");
  });
});

describe("Arguments", () => {
  it("lists named arguments and skips empty ones", () => {
    render(<Arguments value={{ path: "notes.txt", content: "hello", key: null }} />);
    expect(screen.getByText("path")).toBeInTheDocument();
    expect(screen.getByText("notes.txt")).toBeInTheDocument();
    expect(screen.queryByText("key")).not.toBeInTheDocument();
  });
});
