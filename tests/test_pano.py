from PIL import Image

from app.media import pano


def test_angle_pixel_mapping():
    assert pano.angle_to_x(0, 1280) == 0
    assert pano.angle_to_x(180, 1280) == 640
    assert pano.angle_to_x(360, 1280) == 0            # 右边缘与左边缘是同一条经线
    assert abs(pano.x_to_angle(320, 1280) - 90) < 1e-9
    assert pano.norm_deg(-30) == 330


def test_span():
    assert pano.span_deg(150, 210) == 60
    assert pano.span_deg(330, 30) == 60                # 跨缝
    assert pano.span_deg(10, 10) == 360


def test_crop_no_wrap_and_wrap():
    img = pano.synth_pano(1280, 640)
    c = pano.crop_angle_range(img, 150, 210)
    assert abs(c.width - 1280 * 60 / 360) <= 1 and c.height == 640     # 像素取整允许 ±1
    w = pano.crop_angle_range(img, 330, 30)             # 跨缝拼接
    assert abs(w.width - 1280 * 60 // 360) <= 2 and w.height == 640
    full = pano.crop_angle_range(img, 0, 360)
    assert full.size == img.size
    padded = pano.crop_angle_range(img, 150, 210, pad_deg=5)
    assert padded.width > c.width


def test_crop_wrap_content_is_stitched():
    # 左边缘一列红、右边缘一列蓝：跨缝裁切后蓝在左、红在右
    img = Image.new('RGB', (360, 180), (0, 0, 0))
    for y in range(180):
        img.putpixel((0, y), (255, 0, 0))
        img.putpixel((359, y), (0, 0, 255))
    c = pano.crop_angle_range(img, 355, 5)
    assert c.getpixel((4, 10)) == (0, 0, 255)
    assert c.getpixel((5, 10)) == (255, 0, 0)


def test_annotate_and_synth():
    img = pano.synth_pano(640, 320, pose={'x': 1, 'y': 2, 'yaw': 0.3}, door_open=True)
    assert img.size == (640, 320)
    a = pano.annotate(img, 300, 40, forward_deg=180, label='测试')
    assert a.size == img.size
    assert len(pano.to_jpeg(a)) > 1000
