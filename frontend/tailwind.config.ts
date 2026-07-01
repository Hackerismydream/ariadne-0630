import type { Config } from "tailwindcss";

const config: Config = {
  content: ["./app/**/*.{ts,tsx}", "./components/**/*.{ts,tsx}", "./lib/**/*.{ts,tsx}"],
  theme: {
    extend: {
      fontFamily: {
        mono: ["JetBrains Mono", "Fira Code", "VT323", "monospace"],
      },
      colors: {
        terminal: {
          bg: "var(--bg)",
          fg: "var(--fg)",
          primary: "var(--primary)",
          secondary: "var(--secondary)",
          muted: "var(--muted)",
          accent: "var(--accent)",
          error: "var(--error)",
          border: "var(--border)",
        },
      },
      animation: {
        blink: "blink 1s steps(1, end) infinite",
        "type-in": "type-in 260ms steps(18, end)",
      },
    },
  },
  plugins: [],
};

export default config;
