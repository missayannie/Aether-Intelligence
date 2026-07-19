using System.Numerics;
using System.Text;

namespace PortraitSidecar;

// Minimal reader for FFXIV .mdl geometry. Lumina 7.6's MdlFile still parses the
// pre-Dawntrail (V5) layout and throws on every current-patch model, so we
// parse the V6 runtime section ourselves. Layout follows Penumbra.GameData's
// V6 support (bone tables became variable-length header+array in V6).
public sealed class MdlGeometry
{
    public sealed class Mesh
    {
        public required Vector3[] Positions;
        public required Vector3[] Normals;
        public required Vector2[] Uv;
        public required Vector4[] Color;
        public required ushort[] Indices;
        public required string MaterialName;
        public required List<(uint IndexOffset, uint IndexCount, uint AttributeMask)> Submeshes;
    }

    public List<Mesh> Meshes = [];
    public string[] AttributeNames = [];
    public string[] MaterialNames = [];

    private const int Version6 = 0x01000006;

    public static MdlGeometry Parse(byte[] data)
    {
        var r = new BinaryReader(new MemoryStream(data));

        // --- model file header (0x44 bytes) ---
        var version = r.ReadUInt32();
        if (version is not (Version6 or 0x01000005))
            throw new InvalidDataException($"Unsupported mdl version 0x{version:X}");
        r.ReadUInt32(); // stack size
        r.ReadUInt32(); // runtime size
        var vertexDeclarationCount = r.ReadUInt16();
        r.ReadUInt16(); // material count (header copy)
        var vertexOffset = ReadUInt32Array(r, 3);
        var indexOffset = ReadUInt32Array(r, 3);
        ReadUInt32Array(r, 3); // vertex buffer sizes
        ReadUInt32Array(r, 3); // index buffer sizes
        r.ReadByte();          // lod count
        r.ReadBytes(3);        // streaming flag, edge geometry flag, padding

        // --- vertex declarations: always 17 slots of 8 bytes each ---
        var declarations = new List<(byte Stream, byte Offset, byte Type, byte Usage, byte UsageIndex)[]>();
        for (var i = 0; i < vertexDeclarationCount; i++)
        {
            var elements = new List<(byte, byte, byte, byte, byte)>();
            var ended = false;
            for (var e = 0; e < 17; e++)
            {
                var stream = r.ReadByte();
                var offset = r.ReadByte();
                var type = r.ReadByte();
                var usage = r.ReadByte();
                var usageIndex = r.ReadByte();
                r.ReadBytes(3);
                // A stream of 255 terminates the list; later slots are uninitialized.
                if (stream == 255) ended = true;
                if (!ended) elements.Add((stream, offset, type, usage, usageIndex));
            }
            declarations.Add(elements.ToArray());
        }

        // --- string table ---
        var stringCount = r.ReadUInt16();
        r.ReadUInt16();
        var stringSize = r.ReadUInt32();
        var stringBlob = r.ReadBytes((int)stringSize);
        string StringAt(uint offset)
        {
            var end = Array.IndexOf(stringBlob, (byte)0, (int)offset);
            return Encoding.UTF8.GetString(stringBlob, (int)offset, end - (int)offset);
        }

        // --- model header (56 bytes) ---
        r.ReadSingle(); // radius
        var meshCount = r.ReadUInt16();
        var attributeCount = r.ReadUInt16();
        var submeshCount = r.ReadUInt16();
        var materialCount = r.ReadUInt16();
        var boneCount = r.ReadUInt16();
        var boneTableCount = r.ReadUInt16();
        r.ReadUInt16(); // shape count
        r.ReadUInt16(); // shape mesh count
        r.ReadUInt16(); // shape value count
        r.ReadByte();   // lod count
        r.ReadByte();   // flags1
        var elementIdCount = r.ReadUInt16();
        var terrainShadowMeshCount = r.ReadByte();
        var flags2 = r.ReadByte();
        r.ReadSingle(); // model clip out
        r.ReadSingle(); // shadow clip out
        r.ReadUInt16(); // culling grid count
        var terrainShadowSubmeshCount = r.ReadUInt16();
        r.ReadBytes(4); // flags3, bg change material, bg crest change material, neck morph count
        var boneTableArrayCountTotal = r.ReadUInt16();
        r.ReadBytes(4); // unknown8, unknown9
        r.ReadBytes(6); // padding

        r.BaseStream.Position += elementIdCount * 32;

        // --- lods (only LOD0 geometry is used) ---
        var lodMeshIndex = 0;
        var lodMeshCount = 0;
        for (var i = 0; i < 3; i++)
        {
            var meshIndex = r.ReadUInt16();
            var count = r.ReadUInt16();
            if (i == 0) { lodMeshIndex = meshIndex; lodMeshCount = count; }
            r.BaseStream.Position += 60 - 4;
        }

        const byte extraLodFlag = 0x10;
        if ((flags2 & extraLodFlag) != 0)
            r.BaseStream.Position += 3 * 40;

        // --- meshes ---
        var meshes = new (ushort VertexCount, uint IndexCount, ushort MaterialIndex, ushort SubMeshIndex,
            ushort SubMeshCount, uint StartIndex, uint[] BufferOffsets, byte[] Strides)[meshCount];
        for (var i = 0; i < meshCount; i++)
        {
            var vertexCount = r.ReadUInt16();
            r.ReadUInt16();
            var idxCount = r.ReadUInt32();
            var materialIndex = r.ReadUInt16();
            var subMeshIndex = r.ReadUInt16();
            var subMeshCount = r.ReadUInt16();
            r.ReadUInt16(); // bone table index
            var startIndex = r.ReadUInt32();
            var bufferOffsets = ReadUInt32Array(r, 3);
            var strides = r.ReadBytes(3);
            r.ReadByte(); // stream count
            meshes[i] = (vertexCount, idxCount, materialIndex, subMeshIndex, subMeshCount, startIndex, bufferOffsets, strides);
        }

        var result = new MdlGeometry
        {
            AttributeNames = new string[attributeCount],
        };
        for (var i = 0; i < attributeCount; i++)
            result.AttributeNames[i] = StringAt(r.ReadUInt32());

        r.BaseStream.Position += terrainShadowMeshCount * 20;

        var submeshes = new (uint IndexOffset, uint IndexCount, uint AttributeMask)[submeshCount];
        for (var i = 0; i < submeshCount; i++)
        {
            var idxOffset = r.ReadUInt32();
            var idxCount = r.ReadUInt32();
            var attrMask = r.ReadUInt32();
            r.ReadUInt32(); // bone start/count
            submeshes[i] = (idxOffset, idxCount, attrMask);
        }

        r.BaseStream.Position += terrainShadowSubmeshCount * 12;

        result.MaterialNames = new string[materialCount];
        for (var i = 0; i < materialCount; i++)
            result.MaterialNames[i] = StringAt(r.ReadUInt32());

        // Bone data is irrelevant here (models are read in bind pose), but its
        // size depends on version; nothing after it is needed, so stop parsing.
        _ = boneCount;
        _ = boneTableCount;
        _ = boneTableArrayCountTotal;

        // --- geometry for LOD0 meshes ---
        for (var mi = lodMeshIndex; mi < lodMeshIndex + lodMeshCount; mi++)
        {
            var m = meshes[mi];
            var decl = declarations[mi];
            var positions = new Vector3[m.VertexCount];
            var normals = new Vector3[m.VertexCount];
            var uvs = new Vector2[m.VertexCount];
            var colors = new Vector4[m.VertexCount];
            Array.Fill(colors, Vector4.One);

            foreach (var el in decl)
            {
                // Usages: 0 position, 3 normal, 4 uv, 7 color. Skinning data is skipped.
                if (el.Usage is not (0 or 3 or 4) && !(el.Usage == 7 && el.UsageIndex == 0))
                    continue;
                var baseOffset = vertexOffset[0] + m.BufferOffsets[el.Stream];
                var stride = m.Strides[el.Stream];
                for (var v = 0; v < m.VertexCount; v++)
                {
                    r.BaseStream.Position = baseOffset + (long)v * stride + el.Offset;
                    var val = ReadElement(r, el.Type);
                    switch (el.Usage)
                    {
                        case 0: positions[v] = new Vector3(val.X, val.Y, val.Z); break;
                        case 3: normals[v] = new Vector3(val.X, val.Y, val.Z); break;
                        case 4: if (el.UsageIndex == 0) uvs[v] = new Vector2(val.X, val.Y); break;
                        case 7: colors[v] = val; break;
                    }
                }
            }

            r.BaseStream.Position = indexOffset[0] + m.StartIndex * 2;
            var indexData = r.ReadBytes((int)m.IndexCount * 2);
            var indices = new ushort[m.IndexCount];
            Buffer.BlockCopy(indexData, 0, indices, 0, indexData.Length);

            var meshSubmeshes = new List<(uint, uint, uint)>();
            for (var si = m.SubMeshIndex; si < m.SubMeshIndex + m.SubMeshCount; si++)
            {
                var s = submeshes[si];
                // Submesh offsets are absolute into the index buffer; make them mesh-relative.
                meshSubmeshes.Add((s.IndexOffset - m.StartIndex, s.IndexCount, s.AttributeMask));
            }

            result.Meshes.Add(new Mesh
            {
                Positions = positions,
                Normals = normals,
                Uv = uvs,
                Color = colors,
                Indices = indices,
                MaterialName = result.MaterialNames[m.MaterialIndex],
                Submeshes = meshSubmeshes,
            });
        }

        return result;
    }

    private static Vector4 ReadElement(BinaryReader r, byte type) => type switch
    {
        0 => new Vector4(r.ReadSingle(), 0, 0, 0),
        1 => new Vector4(r.ReadSingle(), r.ReadSingle(), 0, 0),
        2 => new Vector4(r.ReadSingle(), r.ReadSingle(), r.ReadSingle(), 0),
        3 => new Vector4(r.ReadSingle(), r.ReadSingle(), r.ReadSingle(), r.ReadSingle()),
        5 => new Vector4(r.ReadByte(), r.ReadByte(), r.ReadByte(), r.ReadByte()),
        6 => new Vector4(r.ReadInt16(), r.ReadInt16(), 0, 0),
        7 => new Vector4(r.ReadInt16(), r.ReadInt16(), r.ReadInt16(), r.ReadInt16()),
        8 => new Vector4(r.ReadByte() / 255f, r.ReadByte() / 255f, r.ReadByte() / 255f, r.ReadByte() / 255f),
        9 => new Vector4(r.ReadInt16() / 32767f, r.ReadInt16() / 32767f, 0, 0),
        10 => new Vector4(r.ReadInt16() / 32767f, r.ReadInt16() / 32767f, r.ReadInt16() / 32767f, r.ReadInt16() / 32767f),
        13 => new Vector4((float)r.ReadHalf(), (float)r.ReadHalf(), 0, 0),
        14 => new Vector4((float)r.ReadHalf(), (float)r.ReadHalf(), (float)r.ReadHalf(), (float)r.ReadHalf()),
        16 => new Vector4(r.ReadUInt16(), r.ReadUInt16(), 0, 0),
        17 => new Vector4(r.ReadUInt16(), r.ReadUInt16(), r.ReadUInt16(), r.ReadUInt16()),
        _ => throw new InvalidDataException($"Unhandled vertex element type {type}"),
    };

    private static uint[] ReadUInt32Array(BinaryReader r, int count)
    {
        var result = new uint[count];
        for (var i = 0; i < count; i++) result[i] = r.ReadUInt32();
        return result;
    }
}
