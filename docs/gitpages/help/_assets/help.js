// In-page search for the help index. No dependencies.
// Filters cards by title + concept tokens; hides sections that drop to zero.
(function () {
  const input = document.getElementById("help-search");
  if (!input) return;

  const cards = Array.from(document.querySelectorAll(".card"));
  const sections = Array.from(document.querySelectorAll(".section"));

  function tokenize(s) {
    return s.toLowerCase().split(/[^a-z0-9]+/).filter(Boolean);
  }

  function score(card, tokens) {
    if (!tokens.length) return 1;
    const haystack = [
      card.dataset.title || "",
      card.dataset.concepts || "",
      card.querySelector("p")?.textContent || "",
    ].join(" ").toLowerCase();
    return tokens.every((t) => haystack.includes(t)) ? 1 : 0;
  }

  function apply() {
    const tokens = tokenize(input.value);
    for (const card of cards) {
      const visible = score(card, tokens) > 0;
      card.classList.toggle("search-hidden", !visible);
    }
    for (const section of sections) {
      const anyVisible = section.querySelectorAll(".card:not(.search-hidden)").length > 0;
      section.classList.toggle("search-empty", !anyVisible);
    }
  }

  let timer = null;
  input.addEventListener("input", () => {
    clearTimeout(timer);
    timer = setTimeout(apply, 80);
  });

  // Focus on `/` like GitHub / many doc sites.
  document.addEventListener("keydown", (e) => {
    if (e.key === "/" && document.activeElement !== input) {
      e.preventDefault();
      input.focus();
      input.select();
    }
  });
})();
