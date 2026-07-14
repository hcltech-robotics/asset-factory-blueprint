(() => {
  "use strict";

  const prefersReducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)");

  const initialise = () => {
    document.documentElement.classList.add("hcl-ready");

    const revealItems = document.querySelectorAll("[data-hcl-reveal]");
    if (prefersReducedMotion.matches || !("IntersectionObserver" in window)) {
      revealItems.forEach((item) => item.classList.add("is-visible"));
    } else {
      const observer = new IntersectionObserver(
        (entries) => {
          entries.forEach((entry) => {
            if (entry.isIntersecting) {
              entry.target.classList.add("is-visible");
              observer.unobserve(entry.target);
            }
          });
        },
        { rootMargin: "0px 0px -8%", threshold: 0.08 },
      );
      revealItems.forEach((item) => observer.observe(item));
    }

    const hero = document.querySelector(".hcl-hero");
    if (hero && !prefersReducedMotion.matches) {
      hero.addEventListener(
        "pointermove",
        (event) => {
          const bounds = hero.getBoundingClientRect();
          const x = ((event.clientX - bounds.left) / bounds.width) * 100;
          const y = ((event.clientY - bounds.top) / bounds.height) * 100;
          hero.style.setProperty("--hcl-pointer-x", `${x.toFixed(1)}%`);
          hero.style.setProperty("--hcl-pointer-y", `${y.toFixed(1)}%`);
        },
        { passive: true },
      );
    }
  };

  if (typeof document$ !== "undefined") {
    document$.subscribe(initialise);
  } else if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", initialise, { once: true });
  } else {
    initialise();
  }
})();
