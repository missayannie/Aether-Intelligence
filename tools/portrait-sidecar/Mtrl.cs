using System.Text;

namespace PortraitSidecar;

// Minimal .mtrl reader: texture paths, shader package name, and the colorset
// table (gear gets nearly all of its color from colorset rows, not textures).
public sealed class MtrlInfo
{
    public string ShaderPackage = "";
    public List<string> TexturePaths = [];
    public Half[] ColorSet = []; // Dawntrail: 32 rows x 32 halves; legacy: 16 x 16

    public static MtrlInfo Parse(byte[] data)
    {
        var r = new BinaryReader(new MemoryStream(data));
        r.ReadUInt32(); // version
        r.ReadUInt16(); // file size
        var colorSetDataSize = r.ReadUInt16();
        var stringTableSize = r.ReadUInt16();
        var shaderPackageNameOffset = r.ReadUInt16();
        var textureCount = r.ReadByte();
        var uvSetCount = r.ReadByte();
        var colorSetCount = r.ReadByte();
        var additionalDataSize = r.ReadByte();

        var textureOffsets = new ushort[textureCount];
        for (var i = 0; i < textureCount; i++)
        {
            textureOffsets[i] = r.ReadUInt16();
            r.ReadUInt16(); // flags
        }
        r.BaseStream.Position += uvSetCount * 4;
        r.BaseStream.Position += colorSetCount * 4;

        var strings = r.ReadBytes(stringTableSize);
        string StringAt(int offset)
        {
            var end = Array.IndexOf(strings, (byte)0, offset);
            return Encoding.UTF8.GetString(strings, offset, end - offset);
        }

        var result = new MtrlInfo { ShaderPackage = StringAt(shaderPackageNameOffset) };
        foreach (var off in textureOffsets)
            result.TexturePaths.Add(StringAt(off));

        r.BaseStream.Position += additionalDataSize;
        if (colorSetDataSize > 0)
        {
            // Dye rows (if any) trail the color rows; only the color rows matter here.
            var halfCount = Math.Min((int)colorSetDataSize, 32 * 64) / 2;
            result.ColorSet = new Half[halfCount];
            for (var i = 0; i < halfCount; i++)
                result.ColorSet[i] = r.ReadHalf();
        }
        return result;
    }
}
