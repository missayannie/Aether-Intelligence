using Lumina;
using PortraitSidecar;

// Renders a bust portrait of an event NPC straight from the installed game
// client's data. Usage: dotnet run -- [enpcId] [outPath] [size]
var gamePath = @"C:\Program Files (x86)\SquareEnix\FINAL FANTASY XIV - A Realm Reborn\game\sqpack";
var npcId = args.Length > 0 ? uint.Parse(args[0]) : 1003988u;
var size = args.Length > 2 ? int.Parse(args[2]) : 512;

var gameData = new GameData(gamePath);
var name = gameData.GetExcelSheet<Lumina.Excel.Sheets.ENpcResident>().GetRow(npcId).Singular.ExtractText();
var safeName = string.Concat(name.ToLowerInvariant().Select(c => char.IsLetterOrDigit(c) ? c : '-'));
var outPath = args.Length > 1 ? args[1] : Path.Combine(AppContext.BaseDirectory, "..", "..", "..", "out", $"{safeName}.png");

new CharacterPortrait(gameData).Render(npcId, Path.GetFullPath(outPath), size);
