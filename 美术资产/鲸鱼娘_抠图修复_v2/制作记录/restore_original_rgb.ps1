param(
    [Parameter(Mandatory=$true)][string]$Source,
    [Parameter(Mandatory=$true)][string]$GeneratedMatte,
    [Parameter(Mandatory=$true)][string]$Output,
    [switch]$RepairExistingRgbaHoles
)
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Drawing
Add-Type -ReferencedAssemblies System.Drawing -TypeDefinition @'
using System;
using System.Drawing;
using System.Drawing.Imaging;
public static class AuraOriginalColorCutout {
    public static string Run(string source, string mask, string output, bool keepExistingRgba) {
        using (var original = new Bitmap(source))
        using (var matte = new Bitmap(mask))
        using (var result = new Bitmap(original.Width, original.Height, PixelFormat.Format32bppArgb)) {
            if (original.Size != matte.Size) throw new ArgumentException("Source and generated matte must align exactly");
            long transparent = 0, opaque = 0, soft = 0, recoveredInk = 0, removedWhite = 0;
            bool sourceHasAlpha = Image.IsAlphaPixelFormat(original.PixelFormat);
            if(keepExistingRgba && (!sourceHasAlpha || original.Width!=1254 || original.Height!=1254))
                throw new ArgumentException("Existing-pose repair expects the verified 1254 RGBA tsundere source");
            var rawMap = new byte[original.Width,original.Height];
            var pixels = new Color[original.Width,original.Height];
            var background = new bool[original.Width,original.Height];
            var queue = new System.Collections.Generic.Queue<int>();
            for (int y = 0; y < original.Height; ++y)
                for (int x = 0; x < original.Width; ++x) {
                    var p = original.GetPixel(x,y); pixels[x,y] = p;
                    rawMap[x,y] = keepExistingRgba ? p.A : matte.GetPixel(x,y).A;
                    int lo = Math.Min(p.R,Math.Min(p.G,p.B)), hi = Math.Max(p.R,Math.Max(p.G,p.B));
                    bool seed = x==0 || y==0 || x==original.Width-1 || y==original.Height-1 || rawMap[x,y]<=12;
                    if (!keepExistingRgba && seed && lo>=225 && hi-lo<=25) {background[x,y]=true;queue.Enqueue(y*original.Width+x);}
                }
            if(keepExistingRgba) {
                foreach(var seed in new[]{new Point(230,660),new Point(140,855)}) {
                    var p=pixels[seed.X,seed.Y]; int lo=Math.Min(p.R,Math.Min(p.G,p.B));
                    if(lo<225) throw new ArgumentException("Verified hair-gap seed is no longer near-white");
                    background[seed.X,seed.Y]=true;queue.Enqueue(seed.Y*original.Width+seed.X);
                }
            }
            // Only near-white regions connected to known background are removed.
            // Character whites enclosed by original ink contours remain intact.
            int[] dx4 = {-1,1,0,0}, dy4 = {0,0,-1,1};
            while (queue.Count>0) {
                int index=queue.Dequeue(), x=index%original.Width, y=index/original.Width;
                for(int d=0;d<4;++d) {
                    int nx=x+dx4[d], ny=y+dy4[d];
                    if(nx<0 || ny<0 || nx>=original.Width || ny>=original.Height || background[nx,ny]) continue;
                    if(keepExistingRgba && (nx<100 || nx>260 || ny<600 || ny>910)) continue;
                    var p=pixels[nx,ny]; int lo=Math.Min(p.R,Math.Min(p.G,p.B)), hi=Math.Max(p.R,Math.Max(p.G,p.B));
                    if(lo>=225 && hi-lo<=25) {background[nx,ny]=true;queue.Enqueue(ny*original.Width+nx);}
                }
            }
            for (int y = 0; y < original.Height; ++y) {
                for (int x = 0; x < original.Width; ++x) {
                    int rawAlpha = rawMap[x,y];
                    // Remove low-alpha model noise; restore the solid foreground to full opacity.
                    int alpha = rawAlpha <= 12 ? 0 : rawAlpha >= 244 ? 255 :
                        (int)Math.Round((rawAlpha - 12) * 255.0 / (244 - 12));
                    var rgb = pixels[x,y];
                    int sourceAlpha = !sourceHasAlpha ? 255 : rgb.A<=12 ? 0 : rgb.A>=244 ? 255 : (int)Math.Round((rgb.A-12)*255.0/232);
                    alpha=Math.Min(alpha,sourceAlpha);
                    int darkest = Math.Min(rgb.R,Math.Min(rgb.G,rgb.B));
                    if (alpha < 255 && darkest < 100 && sourceAlpha>0) {
                        // Recover original dark contour pixels close to the model matte only.
                        // White gaps remain transparent: no foreground dilation or global white key.
                        bool near = false;
                        for (int dy = -4; dy <= 4 && !near; ++dy)
                            for (int dx = -4; dx <= 4; ++dx) {
                                int nx = x+dx, ny = y+dy;
                                if (nx>=0 && ny>=0 && nx<original.Width && ny<original.Height && rawMap[nx,ny]>=244) {near=true;break;}
                            }
                        int inkAlpha = 255-darkest;
                        if (near && inkAlpha > alpha) {alpha=Math.Min(sourceAlpha,inkAlpha);++recoveredInk;}
                    }
                    if(background[x,y]) {if(alpha>0) ++removedWhite;alpha=0;}
                    else if(alpha>0 && darkest>25 && Math.Max(rgb.R,Math.Max(rgb.G,rgb.B))-darkest<=45) {
                        bool edge=false;
                        for(int d=0;d<4;++d) {
                            int nx=x+dx4[d], ny=y+dy4[d];
                            if(nx>=0 && ny>=0 && nx<original.Width && ny<original.Height && background[nx,ny]) {edge=true;break;}
                        }
                        if(edge) alpha=Math.Min(alpha,255-darkest);
                    }
                    if (alpha == 0) { result.SetPixel(x,y, Color.FromArgb(0,0,0,0)); ++transparent; }
                    else if (alpha == 255) { result.SetPixel(x,y,Color.FromArgb(255,rgb.R,rgb.G,rgb.B)); ++opaque; }
                    else {
                        // The source is RGB composited on white. Only soft edge pixels are unmatted.
                        double a = alpha / 255.0;
                        bool unmatte=!sourceHasAlpha || rgb.A>=244;
                        int r = !unmatte ? rgb.R : Math.Max(0,Math.Min(255,(int)Math.Round((rgb.R - 255*(1-a))/a)));
                        int g = !unmatte ? rgb.G : Math.Max(0,Math.Min(255,(int)Math.Round((rgb.G - 255*(1-a))/a)));
                        int b = !unmatte ? rgb.B : Math.Max(0,Math.Min(255,(int)Math.Round((rgb.B - 255*(1-a))/a)));
                        result.SetPixel(x,y, Color.FromArgb(alpha,r,g,b)); ++soft;
                    }
                }
            }
            result.Save(output,ImageFormat.Png);
            return String.Format("width={0}; height={1}; transparent={2}; opaque_original_rgb={3}; soft_edges={4}; original_ink_recovered={5}; connected_background_removed={6}",
                original.Width, original.Height, transparent, opaque, soft,recoveredInk,removedWhite);
        }
    }
}
'@
$auraOutput = [System.IO.Path]::GetFullPath($Output)
if (Test-Path -LiteralPath $auraOutput) { throw 'Choose a new output; existing output is preserved.' }
[void][System.IO.Directory]::CreateDirectory([System.IO.Path]::GetDirectoryName($auraOutput))
[AuraOriginalColorCutout]::Run([System.IO.Path]::GetFullPath($Source),[System.IO.Path]::GetFullPath($GeneratedMatte),$auraOutput,$RepairExistingRgbaHoles.IsPresent)
