using System.Numerics;
using Lumina;
using Lumina.Data.Files;

namespace PortraitSidecar;

// Bilinear sampler over Lumina-decoded texture data (B8G8R8A8).
public sealed class Sampler
{
    private readonly byte[] _data;
    private readonly int _width, _height;

    private Sampler(byte[] bgra, int width, int height)
    {
        _data = bgra;
        _width = width;
        _height = height;
    }

    public static Sampler? Load(GameData gameData, string path)
    {
        // DX11-only textures are stored with a "--" filename prefix.
        if (!gameData.FileExists(path))
        {
            var slash = path.LastIndexOf('/');
            path = path[..(slash + 1)] + "--" + path[(slash + 1)..];
            if (!gameData.FileExists(path)) return null;
        }
        var tex = gameData.GetFile<TexFile>(path)!;
        return new Sampler(tex.ImageData, tex.Header.Width, tex.Header.Height);
    }

    public Vector4 Sample(Vector2 uv)
    {
        var x = (uv.X - MathF.Floor(uv.X)) * _width - 0.5f;
        var y = (uv.Y - MathF.Floor(uv.Y)) * _height - 0.5f;
        var x0 = (int)MathF.Floor(x);
        var y0 = (int)MathF.Floor(y);
        var fx = x - x0;
        var fy = y - y0;
        var c00 = Texel(x0, y0);
        var c10 = Texel(x0 + 1, y0);
        var c01 = Texel(x0, y0 + 1);
        var c11 = Texel(x0 + 1, y0 + 1);
        return Vector4.Lerp(Vector4.Lerp(c00, c10, fx), Vector4.Lerp(c01, c11, fx), fy);
    }

    private Vector4 Texel(int x, int y)
    {
        x = ((x % _width) + _width) % _width;
        y = ((y % _height) + _height) % _height;
        var i = (y * _width + x) * 4;
        return new Vector4(_data[i + 2] / 255f, _data[i + 1] / 255f, _data[i] / 255f, _data[i + 3] / 255f);
    }
}
