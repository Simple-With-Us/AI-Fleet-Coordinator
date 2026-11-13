// The CI and deploy gates glob src/*.test.js, so this pulls in the shared guard's
// .mjs test file, whose name stays the same in every Worker that copies it.
import "./budget-guard.test.mjs";
