/* Hell/Dunkel-Wahl vor dem ersten Zeichnen anwenden (synchron im <head>, ohne eingebettetes Skript). */
try { var t = localStorage.getItem("theme"); if (t) document.documentElement.dataset.theme = t; } catch (e) { /* egal */ }
