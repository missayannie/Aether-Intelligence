using System.Numerics;
using System.Text.RegularExpressions;
using Lumina;
using Lumina.Data;
using Lumina.Excel.Sheets;

namespace PortraitSidecar;

// Assembles an ENpc's character parts (face/hair/gear) from the local game
// client and renders a bust portrait. No skeleton is applied: meshes are
// stored in bind pose, which is fine for a head-and-shoulders crop.
public sealed class CharacterPortrait(GameData gameData)
{
    public bool Verbose = true;

    private sealed record Part(string MdlPath, string Kind, Func<string, bool> AttributeEnabled, Func<string, string?> ResolveMaterial);

    private sealed record ShadedMesh(MdlGeometry.Mesh Mesh, Renderer.PixelShader Shader, bool IsFaceSkin);

    private Vector3 _skinTint = new(0.9f, 0.75f, 0.65f);
    private Vector3 _hairTint = new(0.4f, 0.3f, 0.25f);
    private Vector3 _eyeTint = new(0.3f, 0.35f, 0.4f);

    public void Render(uint npcId, string outPath, int size)
    {
        var npc = gameData.GetExcelSheet<ENpcBase>().GetRow(npcId);
        var name = gameData.GetExcelSheet<ENpcResident>().GetRow(npcId).Singular.ExtractText();
        Log($"NPC {npcId}: {name}");

        if (npc.ModelChara.RowId != 0)
            throw new NotSupportedException($"NPC {npcId} uses a ModelChara (monster-type) model; this experiment only covers human-type NPCs.");

        var raceCode = RaceCode(npc.Race.RowId, npc.Tribe.RowId, npc.Gender);
        Log($"race={npc.Race.RowId} tribe={npc.Tribe.RowId} gender={npc.Gender} -> c{raceCode:D4}, face={npc.Face}, hair={npc.HairStyle}");
        LoadTints(npc);

        var parts = new List<Part>();
        AddFacePart(parts, raceCode, npc);
        AddHairPart(parts, raceCode, npc);
        foreach (var (slot, suffix) in new (uint Value, string Suffix)[]
                 {
                     (Slot(npc, "Head"), "met"),
                     (Slot(npc, "Body"), "top"),
                     (Slot(npc, "Hands"), "glv"),
                     (Slot(npc, "Legs"), "dwn"),
                     (Slot(npc, "Feet"), "sho"),
                 })
            AddGearPart(parts, raceCode, slot, suffix);

        var shaded = new List<ShadedMesh>();
        foreach (var part in parts)
            shaded.AddRange(BuildPart(part));

        RenderMeshes(shaded, outPath, size);
    }

    private uint Slot(ENpcBase npc, string slot)
    {
        uint inline = slot switch
        {
            "Head" => npc.ModelHead,
            "Body" => npc.ModelBody,
            "Hands" => npc.ModelHands,
            "Legs" => npc.ModelLegs,
            "Feet" => npc.ModelFeet,
            _ => 0,
        };
        if (inline != 0) return inline;
        // Some NPCs keep their gear on a shared NpcEquip row instead.
        if (npc.NpcEquip.RowId is 0 or 175 || !npc.NpcEquip.IsValid) return 0;
        var eq = npc.NpcEquip.Value;
        return slot switch
        {
            "Head" => eq.ModelHead,
            "Body" => eq.ModelBody,
            "Hands" => eq.ModelHands,
            "Legs" => eq.ModelLegs,
            "Feet" => eq.ModelFeet,
            _ => 0,
        };
    }

    private static int RaceCode(uint race, uint tribe, byte gender) => race switch
    {
        1 => (tribe == 2 ? 300 : 100) + gender * 100 + 1, // Hyur: Highlander is its own model race
        2 => 501 + gender * 100,
        3 => 1101 + gender * 100,
        4 => 701 + gender * 100,
        5 => 901 + gender * 100,
        6 => 1301 + gender * 100,
        7 => 1501 + gender * 100,
        8 => 1701 + gender * 100,
        _ => throw new NotSupportedException($"Unknown race {race}"),
    };

    private void LoadTints(ENpcBase npc)
    {
        // human.cmp: per (tribe, gender) group of 5 palettes x 256 RGBA colors.
        // Palette roles verified against Garland Tools' rendered NPC colors:
        // 2 = eye, 3 = skin, 4 = hair (0/1 unidentified, likely highlights/etc).
        var cmp = gameData.GetFile<FileResource>("chara/xls/charamake/human.cmp")!.Data;
        var group = ((int)npc.Tribe.RowId - 1) * 2 + npc.Gender;
        Vector3 Palette(int palette, int index)
        {
            var o = (group * 1280 + palette * 256 + index) * 4;
            return SrgbToLinear(new Vector3(cmp[o] / 255f, cmp[o + 1] / 255f, cmp[o + 2] / 255f));
        }
        _skinTint = Palette(3, npc.SkinColor);
        _hairTint = Palette(4, npc.HairColor);
        _eyeTint = Palette(2, npc.EyeColor);
        Log($"tints: skin={FmtColor(_skinTint)} hair={FmtColor(_hairTint)} eye={FmtColor(_eyeTint)}");
    }

    private void AddFacePart(List<Part> parts, int raceCode, ENpcBase npc)
    {
        var face = npc.Face;
        var dir = $"chara/human/c{raceCode:D4}/obj/face/f{face:D4}";
        var mdl = $"{dir}/model/c{raceCode:D4}f{face:D4}_fac.mdl";
        if (!gameData.FileExists(mdl))
        {
            Log($"face model missing: {mdl}; trying f0001");
            dir = $"chara/human/c{raceCode:D4}/obj/face/f0001";
            mdl = $"{dir}/model/c{raceCode:D4}f0001_fac.mdl";
        }
        var features = npc.FacialFeature;
        parts.Add(new Part(mdl, "face",
            attr => attr switch
            {
                _ when Regex.Match(attr, "^atr_fv_([a-h])$") is { Success: true } m
                    => (features & 1 << (m.Groups[1].Value[0] - 'a')) != 0,
                "atr_lod" => false,
                _ => true, // ears (atr_mim) and other unconditional face parts
            },
            name => FirstExisting($"{dir}/material{name}", $"{dir}/material/v0001{name}")));
    }

    private void AddHairPart(List<Part> parts, int raceCode, ENpcBase npc)
    {
        var hair = (int)npc.HairStyle;
        if (hair == 0) return;
        // NPC-only hairstyles are not authored for every race; fall back through
        // the shared bases rather than rendering bald.
        int[] chain = [raceCode, raceCode % 200 > 100 ? raceCode - 100 : raceCode, npc.Gender == 1 ? 201 : 101];
        foreach (var rc in chain.Distinct())
        {
            var dir = $"chara/human/c{rc:D4}/obj/hair/h{hair:D4}";
            var mdl = $"{dir}/model/c{rc:D4}h{hair:D4}_hir.mdl";
            if (!gameData.FileExists(mdl)) continue;
            parts.Add(new Part(mdl, "hair",
                attr => attr != "atr_lod",
                name => FirstExisting($"{dir}/material/v0001{name}", $"{dir}/material{name}")));
            return;
        }
        Log($"WARN: no hair model found for h{hair:D4}");
    }

    private void AddGearPart(List<Part> parts, int raceCode, uint packed, string suffix)
    {
        if (packed == 0) return;
        var set = (int)(packed & 0xFFFF);
        var variant = (int)(packed >> 16 & 0xFF);
        if (set is 0 or 0xFFFF) return;

        // Equipment is only authored for a handful of base races; the runtime
        // deforms the nearest match. Without the bone deformer the Lalafell male
        // base (shared by both genders) is still exact for Lalafell NPCs; for
        // everyone else fall through the same-gender Hyur base first.
        var female = raceCode / 100 % 2 == 0;
        int[] chain = raceCode is 1101 or 1201
            ? [raceCode, 1101, 101]
            : [raceCode, female ? 201 : 101, 101];
        string? mdl = null;
        foreach (var rc in chain.Distinct())
        {
            var candidate = $"chara/equipment/e{set:D4}/model/c{rc:D4}e{set:D4}_{suffix}.mdl";
            if (gameData.FileExists(candidate)) { mdl = candidate; break; }
        }
        if (mdl == null)
        {
            Log($"WARN: no model for e{set:D4} {suffix}");
            return;
        }

        var (materialId, attrMask) = ReadImc(set, suffix, variant);
        Log($"gear {suffix}: e{set:D4} v{variant} -> {mdl} (material v{materialId:D4}, attrs 0x{attrMask:X})");

        parts.Add(new Part(mdl, "gear",
            attr => attr switch
            {
                "atr_lod" => false,
                _ when Regex.Match(attr, "^atr_[a-z]{2}_([a-j])$") is { Success: true } m
                    => (attrMask & 1 << (m.Groups[1].Value[0] - 'a')) != 0,
                _ => true, // skin-region toggles (atr_nek/atr_ude/...) stay on with no accessories
            },
            name =>
            {
                var m = Regex.Match(name, @"mt_(c\d{4})b(\d{4})_");
                if (m.Success) // gear meshes with skin material live in the body part tree
                    return FirstExisting(
                        $"chara/human/{m.Groups[1].Value}/obj/body/b{m.Groups[2].Value}/material/v0001{name}",
                        $"chara/human/{m.Groups[1].Value}/obj/body/b{m.Groups[2].Value}/material{name}");
                return FirstExisting($"chara/equipment/e{set:D4}/material/v{materialId:D4}{name}");
            }));
    }

    private (int MaterialId, int AttrMask) ReadImc(int set, string suffix, int variant)
    {
        var path = $"chara/equipment/e{set:D4}/e{set:D4}.imc";
        if (!gameData.FileExists(path)) return (1, 0x3FF);
        var imc = gameData.GetFile<FileResource>(path)!.Data;
        var count = BitConverter.ToUInt16(imc, 0);
        var part = suffix switch { "met" => 0, "top" => 1, "glv" => 2, "dwn" => 3, "sho" => 4, _ => 1 };
        var v = Math.Min(variant, (int)count);
        var off = 4 + (v * 5 + part) * 6;
        if (off + 6 > imc.Length) return (1, 0x3FF);
        return (imc[off], BitConverter.ToUInt16(imc, off + 2) & 0x3FF);
    }

    private string? FirstExisting(params string[] candidates)
        => candidates.FirstOrDefault(gameData.FileExists);

    private List<ShadedMesh> BuildPart(Part part)
    {
        var result = new List<ShadedMesh>();
        var geo = MdlGeometry.Parse(gameData.GetFile<FileResource>(part.MdlPath)!.Data);

        foreach (var mesh in geo.Meshes)
        {
            // Drop submeshes whose attributes are disabled (facial feature
            // variants, gear style variants for other IMC variants, LOD parts).
            var keptIndices = new List<ushort>();
            foreach (var (offset, count, mask) in mesh.Submeshes)
            {
                var visible = true;
                for (var bit = 0; bit < geo.AttributeNames.Length; bit++)
                    if ((mask & 1u << bit) != 0 && !part.AttributeEnabled(geo.AttributeNames[bit]))
                        visible = false;
                if (!visible) continue;
                for (var i = 0; i < count; i++)
                    keptIndices.Add(mesh.Indices[offset + i]);
            }
            if (mesh.Submeshes.Count == 0) keptIndices.AddRange(mesh.Indices);
            if (keptIndices.Count == 0) continue;

            var filtered = new MdlGeometry.Mesh
            {
                Positions = mesh.Positions,
                Normals = mesh.Normals,
                Uv = mesh.Uv,
                Color = mesh.Color,
                Indices = keptIndices.ToArray(),
                MaterialName = mesh.MaterialName,
                Submeshes = [],
            };

            var resolved = part.ResolveMaterial(mesh.MaterialName);
            if (resolved == null)
            {
                Log($"WARN: unresolved material {mesh.MaterialName} in {part.MdlPath}");
                result.Add(new ShadedMesh(filtered, FlatShader(new Vector3(0.5f, 0.5f, 0.5f)), false));
                continue;
            }

            var mtrl = MtrlInfo.Parse(gameData.GetFile<FileResource>(resolved)!.Data);
            var shader = BuildShader(mtrl, part.Kind, out var skip);
            if (skip) continue;
            var isFaceSkin = part.Kind == "face" && mtrl.ShaderPackage == "skin.shpk";
            result.Add(new ShadedMesh(filtered, shader, isFaceSkin));
        }
        return result;
    }

    private Renderer.PixelShader BuildShader(MtrlInfo mtrl, string kind, out bool skip)
    {
        skip = false;
        Sampler? diffuse = null, normal = null, mask = null, id = null;
        foreach (var texPath in mtrl.TexturePaths)
        {
            if (texPath.Contains("catchlight")) continue;
            var stem = Path.GetFileNameWithoutExtension(texPath);
            var suffix = stem[(stem.LastIndexOf('_') + 1)..];
            var sampler = Sampler.Load(gameData, texPath);
            if (sampler == null) continue;
            switch (suffix)
            {
                case "base" or "d": diffuse = sampler; break;
                case "norm" or "n": normal = sampler; break;
                case "mask" or "m" or "s" or "spec": mask = sampler; break;
                case "id": id = sampler; break;
            }
        }

        switch (mtrl.ShaderPackage)
        {
            // Occlusion/tattoo layers are multiplicative decals; omitting them
            // loses subtle shading but never adds artifacts.
            case "characterocclusion.shpk" or "charactertattoo.shpk":
                skip = true;
                return FlatShader(Vector3.Zero);

            case "skin.shpk":
            {
                var tint = _skinTint;
                return (uv, n, _) =>
                {
                    var albedo = tint;
                    if (diffuse != null)
                    {
                        var d = diffuse.Sample(uv);
                        albedo = SrgbToLinear(new Vector3(d.X, d.Y, d.Z)) * tint * 1.35f;
                    }
                    return Light(albedo, n, 1f);
                };
            }

            case "hair.shpk":
            {
                var tint = _hairTint;
                return (uv, n, _) =>
                {
                    var opacity = normal?.Sample(uv).W ?? 1f;
                    var occ = mask?.Sample(uv).X ?? 0.7f;
                    var albedo = tint * (0.35f + 0.65f * occ);
                    return Light(albedo, n, opacity);
                };
            }

            case "iris.shpk":
            {
                var tint = _eyeTint;
                return (uv, n, _) =>
                {
                    var d = diffuse?.Sample(uv) ?? Vector4.One;
                    var albedo = SrgbToLinear(new Vector3(d.X, d.Y, d.Z)) * tint * 2.0f;
                    return Light(albedo, n, 1f);
                };
            }

            default: // character.shpk / characterlegacy.shpk / characterglass.shpk gear
            {
                var colorSet = mtrl.ColorSet;
                var dtRows = colorSet.Length >= 1024;   // 32 rows x 32 halves vs legacy 16 x 16
                var stride = dtRows ? 32 : 16;
                var rowCount = dtRows ? 32 : 16;
                Vector3 Row(int r)
                {
                    r = Math.Clamp(r, 0, rowCount - 1);
                    return new Vector3((float)colorSet[r * stride], (float)colorSet[r * stride + 1], (float)colorSet[r * stride + 2]);
                }
                return (uv, n, _) =>
                {
                    var albedo = Vector3.One;
                    var opacity = 1f;
                    if (colorSet.Length > 0)
                    {
                        if (id != null)
                        {
                            // Dawntrail id map: R selects the row pair, G blends within it.
                            var s = id.Sample(uv);
                            var pair = (int)MathF.Round(s.X * 15f);
                            albedo = Vector3.Lerp(Row(pair * 2), Row(pair * 2 + (dtRows ? 1 : 0)), s.Y);
                            opacity = normal?.Sample(uv).W ?? 1f;
                        }
                        else if (normal != null)
                        {
                            // Legacy: normal alpha selects the row, blue is opacity.
                            var s = normal.Sample(uv);
                            var v = s.W * (rowCount / 2 - 1);
                            var lo = (int)MathF.Floor(v);
                            albedo = Vector3.Lerp(Row(lo * 2), Row(Math.Min(lo + 1, rowCount / 2 - 1) * 2), v - lo);
                            opacity = s.Z;
                        }
                    }
                    if (diffuse != null)
                    {
                        var d = diffuse.Sample(uv);
                        albedo *= SrgbToLinear(new Vector3(d.X, d.Y, d.Z));
                        if (id == null && normal == null) opacity = d.W;
                    }
                    return Light(albedo, n, opacity);
                };
            }
        }
    }

    private static Renderer.PixelShader FlatShader(Vector3 color)
        => (_, n, _) => Light(color, n, 1f);

    private static readonly Vector3 KeyLight = Vector3.Normalize(new Vector3(0.45f, 0.4f, 0.8f));
    private static readonly Vector3 FillLight = Vector3.Normalize(new Vector3(-0.6f, 0.1f, -0.5f));
    private static Vector3 _lightBasis = Vector3.UnitZ; // rotated to face the camera at render time

    private static Vector4 Light(Vector3 albedo, Vector3 n, float alpha)
    {
        // Rotate the canonical +Z-facing light rig toward the model's actual facing.
        var forward = _lightBasis;
        var right = Vector3.Normalize(Vector3.Cross(Vector3.UnitY, forward));
        Vector3 Rig(Vector3 l) => Vector3.Normalize(right * l.X + Vector3.UnitY * l.Y + forward * l.Z);
        var intensity = 0.38f
                        + 0.72f * MathF.Max(0, Vector3.Dot(n, Rig(KeyLight)))
                        + 0.22f * MathF.Max(0, Vector3.Dot(n, Rig(FillLight)));
        var c = albedo * intensity;
        return new Vector4(c.X, c.Y, c.Z, alpha);
    }

    private void RenderMeshes(List<ShadedMesh> meshes, string outPath, int size)
    {
        // Frame on the head: face-skin meshes define the anchor bbox.
        var min = new Vector3(float.MaxValue);
        var max = new Vector3(float.MinValue);
        var avgNormal = Vector3.Zero;
        foreach (var sm in meshes.Where(m => m.IsFaceSkin))
        {
            foreach (var p in sm.Mesh.Positions)
            {
                min = Vector3.Min(min, p);
                max = Vector3.Max(max, p);
            }
            foreach (var n in sm.Mesh.Normals) avgNormal += n;
        }
        if (min.X > max.X) throw new InvalidOperationException("No face mesh found to frame the portrait.");

        var center = (min + max) / 2;
        var headHeight = MathF.Max(max.Y - min.Y, 0.05f);
        var facing = avgNormal with { Y = 0 };
        facing = facing.LengthSquared() < 1e-6f ? Vector3.UnitZ : Vector3.Normalize(facing);
        _lightBasis = facing;

        const float fov = 18f;
        var frameHeight = headHeight * 2.55f; // head plus hair above and shoulders below
        var distance = frameHeight / 2 / MathF.Tan(fov / 2 * MathF.PI / 180f);
        var target = center - new Vector3(0, headHeight * 0.22f, 0);
        var eye = target + facing * distance + new Vector3(0, headHeight * 0.12f, 0);
        Log($"head bbox y=[{min.Y:F3},{max.Y:F3}] center={center:F3} facing={facing:F2}");

        var ss = 2; // supersample then box-filter down
        var renderer = new Renderer(size * ss, size * ss);
        renderer.SetCamera(eye, target, fov);

        var triCount = 0;
        foreach (var sm in meshes)
        {
            var m = sm.Mesh;
            for (var i = 0; i + 2 < m.Indices.Length; i += 3)
            {
                var (a, b, c) = (m.Indices[i], m.Indices[i + 1], m.Indices[i + 2]);
                renderer.Draw(new Renderer.Tri
                {
                    P0 = m.Positions[a], P1 = m.Positions[b], P2 = m.Positions[c],
                    N0 = m.Normals[a], N1 = m.Normals[b], N2 = m.Normals[c],
                    T0 = m.Uv[a], T1 = m.Uv[b], T2 = m.Uv[c],
                    C0 = m.Color[a], C1 = m.Color[b], C2 = m.Color[c],
                    Shader = sm.Shader,
                });
                triCount++;
            }
        }
        Log($"rendered {triCount} triangles from {meshes.Count} meshes");

        var hi = renderer.ToRgba();
        var lo = Downsample(hi, size * ss, size * ss, ss);
        Directory.CreateDirectory(Path.GetDirectoryName(Path.GetFullPath(outPath))!);
        Png.Write(outPath, lo, size, size);
        Log($"wrote {outPath}");
    }

    private static byte[] Downsample(byte[] rgba, int width, int height, int factor)
    {
        var ow = width / factor;
        var oh = height / factor;
        var result = new byte[ow * oh * 4];
        Span<int> acc = stackalloc int[4];
        for (var y = 0; y < oh; y++)
        for (var x = 0; x < ow; x++)
        {
            acc.Clear();
            for (var dy = 0; dy < factor; dy++)
            for (var dx = 0; dx < factor; dx++)
            {
                var i = ((y * factor + dy) * width + x * factor + dx) * 4;
                for (var c = 0; c < 4; c++) acc[c] += rgba[i + c];
            }
            var o = (y * ow + x) * 4;
            for (var c = 0; c < 4; c++) result[o + c] = (byte)(acc[c] / (factor * factor));
        }
        return result;
    }

    private static Vector3 SrgbToLinear(Vector3 c)
        => new(MathF.Pow(c.X, 2.2f), MathF.Pow(c.Y, 2.2f), MathF.Pow(c.Z, 2.2f));

    private static string FmtColor(Vector3 linear)
    {
        Vector3 s = new(MathF.Pow(linear.X, 1 / 2.2f), MathF.Pow(linear.Y, 1 / 2.2f), MathF.Pow(linear.Z, 1 / 2.2f));
        return $"#{(int)(s.X * 255):X2}{(int)(s.Y * 255):X2}{(int)(s.Z * 255):X2}";
    }

    private void Log(string message)
    {
        if (Verbose) Console.WriteLine(message);
    }
}
