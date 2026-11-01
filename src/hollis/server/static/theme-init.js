// Applies the saved theme before first paint (an external file: the CSP forbids inline scripts).
try {
  var theme = localStorage.getItem("hollis-theme");
  if (theme === "light" || theme === "dark") document.documentElement.dataset.theme = theme;
} catch {
  // storage unavailable: follow the system setting
}
