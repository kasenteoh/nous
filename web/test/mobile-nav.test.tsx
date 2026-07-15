// Behaviour tests for the mobile masthead menu (components/MobileNav.tsx):
// collapsed by default, opens to the full PRIMARY_NAV list, and closes on link
// click / Escape.

import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { MobileNav } from "@/components/MobileNav";
import { PRIMARY_NAV } from "@/lib/nav";

describe("MobileNav", () => {
  it("is collapsed by default — no menu, button not expanded", () => {
    render(<MobileNav />);
    const button = screen.getByRole("button", { name: "Open menu" });
    expect(button).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByRole("navigation", { name: "Primary" })).toBeNull();
  });

  it("opens to the full PRIMARY_NAV list and reflects the expanded state", () => {
    render(<MobileNav />);
    fireEvent.click(screen.getByRole("button", { name: "Open menu" }));

    expect(
      screen.getByRole("navigation", { name: "Primary" }),
    ).toBeInTheDocument();
    for (const item of PRIMARY_NAV) {
      expect(screen.getByRole("link", { name: item.label })).toHaveAttribute(
        "href",
        item.href,
      );
    }
    expect(
      screen.getByRole("button", { name: "Close menu" }),
    ).toHaveAttribute("aria-expanded", "true");
  });

  it("closes when a link is followed", () => {
    render(<MobileNav />);
    fireEvent.click(screen.getByRole("button", { name: "Open menu" }));
    fireEvent.click(screen.getByRole("link", { name: "Browse" }));
    expect(screen.queryByRole("navigation", { name: "Primary" })).toBeNull();
  });

  it("closes on Escape", () => {
    render(<MobileNav />);
    fireEvent.click(screen.getByRole("button", { name: "Open menu" }));
    fireEvent.keyDown(document, { key: "Escape" });
    expect(screen.queryByRole("navigation", { name: "Primary" })).toBeNull();
  });
});
