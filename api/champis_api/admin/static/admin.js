/* Backoffice Champis (lot BO6) : seul script maison, facultatif. Les pages marchent sans lui ;
   il n'ajoute que le bouton « Copier » des prompts (les éléments [data-js] n'existent que pour lui). */
"use strict";
document.addEventListener("click", async (event) => {
  const button = event.target.closest("[data-copy]");
  if (!button) return;
  const source = document.getElementById(button.dataset.copy);
  if (!source) return;
  const label = button.textContent;
  try {
    await navigator.clipboard.writeText(source.textContent);
    button.textContent = "Copié";
  } catch (_) {
    button.textContent = "Copie impossible : sélectionne le texte";
  }
  setTimeout(() => { button.textContent = label; }, 1500);
});
