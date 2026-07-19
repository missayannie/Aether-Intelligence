using System.Numerics;

namespace PortraitSidecar;

// Tiny perspective-correct software rasterizer. Headless by design: an
// offscreen D3D context is the classic point of failure for a sidecar like
// this, and a few hundred thousand triangles at 512px is trivial on CPU.
public sealed class Renderer
{
    public delegate Vector4 PixelShader(Vector2 uv, Vector3 normal, Vector4 vertexColor);

    public readonly struct Tri
    {
        public required Vector3 P0 { get; init; }
        public required Vector3 P1 { get; init; }
        public required Vector3 P2 { get; init; }
        public required Vector3 N0 { get; init; }
        public required Vector3 N1 { get; init; }
        public required Vector3 N2 { get; init; }
        public required Vector2 T0 { get; init; }
        public required Vector2 T1 { get; init; }
        public required Vector2 T2 { get; init; }
        public required Vector4 C0 { get; init; }
        public required Vector4 C1 { get; init; }
        public required Vector4 C2 { get; init; }
        public required PixelShader Shader { get; init; }
    }

    private readonly int _width, _height;
    private readonly float[] _depth;
    private readonly Vector4[] _color;
    private Matrix4x4 _viewProj;

    public Renderer(int width, int height)
    {
        _width = width;
        _height = height;
        _depth = new float[width * height];
        _color = new Vector4[width * height];
        Array.Fill(_depth, float.MaxValue);
    }

    public void SetCamera(Vector3 eye, Vector3 target, float fovDegrees)
    {
        var view = Matrix4x4.CreateLookAt(eye, target, Vector3.UnitY);
        var proj = Matrix4x4.CreatePerspectiveFieldOfView(fovDegrees * MathF.PI / 180f, (float)_width / _height, 0.01f, 100f);
        _viewProj = view * proj;
    }

    public void Draw(Tri t)
    {
        var h0 = Vector4.Transform(new Vector4(t.P0, 1), _viewProj);
        var h1 = Vector4.Transform(new Vector4(t.P1, 1), _viewProj);
        var h2 = Vector4.Transform(new Vector4(t.P2, 1), _viewProj);
        if (h0.W <= 0 || h1.W <= 0 || h2.W <= 0) return; // skip near-plane clipping; camera never sits inside the bust

        Vector3 ToScreen(Vector4 h) => new(
            (h.X / h.W * 0.5f + 0.5f) * _width,
            (1 - (h.Y / h.W * 0.5f + 0.5f)) * _height,
            h.Z / h.W);
        var s0 = ToScreen(h0);
        var s1 = ToScreen(h1);
        var s2 = ToScreen(h2);

        var minX = Math.Max(0, (int)MathF.Floor(MathF.Min(s0.X, MathF.Min(s1.X, s2.X))));
        var maxX = Math.Min(_width - 1, (int)MathF.Ceiling(MathF.Max(s0.X, MathF.Max(s1.X, s2.X))));
        var minY = Math.Max(0, (int)MathF.Floor(MathF.Min(s0.Y, MathF.Min(s1.Y, s2.Y))));
        var maxY = Math.Min(_height - 1, (int)MathF.Ceiling(MathF.Max(s0.Y, MathF.Max(s1.Y, s2.Y))));
        if (minX > maxX || minY > maxY) return;

        var area = Edge(s0, s1, s2);
        if (MathF.Abs(area) < 1e-9f) return;

        var w0Inv = 1f / h0.W;
        var w1Inv = 1f / h1.W;
        var w2Inv = 1f / h2.W;

        for (var y = minY; y <= maxY; y++)
        for (var x = minX; x <= maxX; x++)
        {
            var p = new Vector3(x + 0.5f, y + 0.5f, 0);
            var b0 = Edge(s1, s2, p) / area;
            var b1 = Edge(s2, s0, p) / area;
            var b2 = Edge(s0, s1, p) / area;
            // Accept either winding: gear/face meshes are not consistently wound
            // relative to each other, and the z-buffer resolves occlusion anyway.
            if (b0 < 0 || b1 < 0 || b2 < 0)
            {
                if (b0 > 0 || b1 > 0 || b2 > 0) continue;
                b0 = -b0; b1 = -b1; b2 = -b2;
            }

            var z = b0 * s0.Z + b1 * s1.Z + b2 * s2.Z;
            var idx = y * _width + x;
            if (z >= _depth[idx]) continue;

            // Perspective-correct interpolation.
            var pw = b0 * w0Inv + b1 * w1Inv + b2 * w2Inv;
            var k0 = b0 * w0Inv / pw;
            var k1 = b1 * w1Inv / pw;
            var k2 = b2 * w2Inv / pw;

            var uv = k0 * t.T0 + k1 * t.T1 + k2 * t.T2;
            var n = Vector3.Normalize(k0 * t.N0 + k1 * t.N1 + k2 * t.N2);
            var vc = k0 * t.C0 + k1 * t.C1 + k2 * t.C2;

            var c = t.Shader(uv, n, vc);
            if (c.W < 0.5f) continue; // alpha cutout

            _depth[idx] = z;
            _color[idx] = new Vector4(c.X, c.Y, c.Z, 1f);
        }
    }

    private static float Edge(Vector3 a, Vector3 b, Vector3 c)
        => (c.X - a.X) * (b.Y - a.Y) - (c.Y - a.Y) * (b.X - a.X);

    public byte[] ToRgba()
    {
        var result = new byte[_width * _height * 4];
        for (var i = 0; i < _color.Length; i++)
        {
            var c = _color[i];
            result[i * 4 + 0] = (byte)Math.Clamp(MathF.Pow(Math.Clamp(c.X, 0, 1), 1 / 2.2f) * 255f, 0, 255);
            result[i * 4 + 1] = (byte)Math.Clamp(MathF.Pow(Math.Clamp(c.Y, 0, 1), 1 / 2.2f) * 255f, 0, 255);
            result[i * 4 + 2] = (byte)Math.Clamp(MathF.Pow(Math.Clamp(c.Z, 0, 1), 1 / 2.2f) * 255f, 0, 255);
            result[i * 4 + 3] = (byte)Math.Clamp(c.W * 255f, 0, 255);
        }
        return result;
    }
}
