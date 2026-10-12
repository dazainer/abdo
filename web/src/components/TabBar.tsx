import { NavLink } from "react-router";
import { Icon, type IconName } from "./Icon";

const TABS: { to: string; label: string; icon: IconName }[] = [
  { to: "/", label: "Chat", icon: "chat" },
  { to: "/lists", label: "Lists", icon: "list" },
  { to: "/today", label: "Today", icon: "calendar" },
  { to: "/settings", label: "Settings", icon: "settings" },
];

export function TabBar() {
  return (
    <nav className="tabbar" aria-label="Main">
      <div className="tabbar-inner">
        {TABS.map((t) => (
          <NavLink key={t.to} to={t.to} end className={({ isActive }) => `tab${isActive ? " active" : ""}`}>
            <Icon name={t.icon} />
            <span>{t.label}</span>
          </NavLink>
        ))}
      </div>
    </nav>
  );
}
