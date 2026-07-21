import type { Config } from "tailwindcss";

const config: Config = {
  content: [
    "./app/**/*.{ts,tsx}",
    "./components/**/*.{ts,tsx}",
    "./lib/**/*.{ts,tsx}",
  ],
  theme: {
    extend: {
      colors: {
        bg: "#0b0f17",
        panel: "#131a26",
        panel2: "#1a2333",
        edge: "#243044",
        accent: "#4f9dff",
        good: "#34d399",
        bad: "#f87171",
        warn: "#fbbf24",
        muted: "#8595ad",
      },
    },
  },
  plugins: [],
};

export default config;
