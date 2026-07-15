// Single source of truth for the masthead's primary navigation. Shared by the
// desktop nav (server-rendered in layout.tsx) and the mobile menu
// (components/MobileNav.tsx) so the two never drift.

export interface NavItem {
  href: string;
  label: string;
}

export const PRIMARY_NAV: readonly NavItem[] = [
  { href: "/companies", label: "Browse" },
  { href: "/investors", label: "Investors" },
  { href: "/themes", label: "Themes" },
  { href: "/industry", label: "Industries" },
  { href: "/trends", label: "Trends" },
  { href: "/trending", label: "Heating up" },
  { href: "/surprise", label: "Surprise me" },
  { href: "/about", label: "About" },
];
