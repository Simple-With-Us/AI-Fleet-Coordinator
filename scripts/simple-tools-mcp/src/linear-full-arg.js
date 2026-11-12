/** Shared `full` schema for compact list/search read tools (AFC-756). */
export const FULL_ARG = {
  type: "boolean",
  default: false,
  description:
    "Return the full lossless payload.  The default is compact:  at most ten rows and text fields cut to about 280 characters.",
};
