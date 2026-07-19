// turndown-plugin-gfm ships no types and has no @types package on npm, so declare
// the bits we use. It's what makes tables survive a save in the doc editor — without
// it turndown flattens a <table> into a list of bare cell values.
declare module "turndown-plugin-gfm" {
  import type TurndownService from "turndown";
  /** Everything below (tables + strikethrough + task lists). */
  export const gfm: TurndownService.Plugin;
  export const tables: TurndownService.Plugin;
  export const strikethrough: TurndownService.Plugin;
  export const taskListItems: TurndownService.Plugin;
  export const highlightedCodeBlock: TurndownService.Plugin;
}
