using System;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Media;

namespace Gravelle.Windows
{
    // WPF's Border can round a 1-DIP edge differently across a rounded box.
    // Use the same whole-pixel thickness on every edge, including at 150%.
    public sealed class ControlOutline : Decorator
    {
        public static readonly DependencyProperty BackgroundProperty = DependencyProperty.Register(
            "Background", typeof(Brush), typeof(ControlOutline),
            new FrameworkPropertyMetadata(null, FrameworkPropertyMetadataOptions.AffectsRender));

        public static readonly DependencyProperty OutlineBrushProperty = DependencyProperty.Register(
            "OutlineBrush", typeof(Brush), typeof(ControlOutline),
            new FrameworkPropertyMetadata(null, FrameworkPropertyMetadataOptions.AffectsRender));

        public static readonly DependencyProperty RadiusProperty = DependencyProperty.Register(
            "Radius", typeof(double), typeof(ControlOutline),
            new FrameworkPropertyMetadata(8.0, FrameworkPropertyMetadataOptions.AffectsRender));

        public Brush Background
        {
            get { return (Brush)GetValue(BackgroundProperty); }
            set { SetValue(BackgroundProperty, value); }
        }

        public Brush OutlineBrush
        {
            get { return (Brush)GetValue(OutlineBrushProperty); }
            set { SetValue(OutlineBrushProperty, value); }
        }

        public double Radius
        {
            get { return (double)GetValue(RadiusProperty); }
            set { SetValue(RadiusProperty, value); }
        }

        protected override void OnDpiChanged(DpiScale oldDpi, DpiScale newDpi)
        {
            base.OnDpiChanged(oldDpi, newDpi);
            InvalidateVisual();
        }

        protected override void OnRender(DrawingContext drawing)
        {
            DpiScale dpi = VisualTreeHelper.GetDpi(this);
            double width = Math.Round(RenderSize.Width * dpi.DpiScaleX) / dpi.DpiScaleX;
            double height = Math.Round(RenderSize.Height * dpi.DpiScaleY) / dpi.DpiScaleY;
            if (width <= 0 || height <= 0) return;

            double edgeX = Math.Max(1, Math.Round(dpi.DpiScaleX, MidpointRounding.AwayFromZero)) / dpi.DpiScaleX;
            double edgeY = Math.Max(1, Math.Round(dpi.DpiScaleY, MidpointRounding.AwayFromZero)) / dpi.DpiScaleY;
            double radius = Math.Min(Radius, Math.Min(width, height) / 2);
            var outer = new RectangleGeometry(new Rect(0, 0, width, height), radius, radius);
            drawing.DrawGeometry(Background, null, outer);
            if (OutlineBrush == null || width <= edgeX * 2 || height <= edgeY * 2) return;

            var inner = new RectangleGeometry(new Rect(edgeX, edgeY, width - edgeX * 2, height - edgeY * 2),
                Math.Max(0, radius - edgeX), Math.Max(0, radius - edgeY));
            drawing.DrawGeometry(OutlineBrush, null, new CombinedGeometry(GeometryCombineMode.Exclude, outer, inner));
        }
    }
}
