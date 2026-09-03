import numpy as np

from app.media.pointcloud import PointCloudProvider, read_pcd, read_ply, voxel_downsample


def _write_pcd(path, pts, binary=False):
    hdr = (f'# .PCD v0.7\nVERSION 0.7\nFIELDS x y z\nSIZE 4 4 4\nTYPE F F F\nCOUNT 1 1 1\nWIDTH {len(pts)}\nHEIGHT 1\n'
           f'VIEWPOINT 0 0 0 1 0 0 0\nPOINTS {len(pts)}\nDATA {"binary" if binary else "ascii"}\n')
    with open(path, 'wb') as f:
        f.write(hdr.encode())
        if binary:
            f.write(np.asarray(pts, dtype='<f4').tobytes())
        else:
            f.write('\n'.join(' '.join(f'{v:.4f}' for v in p) for p in pts).encode() + b'\n')


def test_read_pcd_ascii_and_binary(tmp_path):
    pts = np.random.RandomState(0).rand(500, 3) * 10
    _write_pcd(tmp_path / 'a.pcd', pts)
    _write_pcd(tmp_path / 'b.pcd', pts, binary=True)
    a, b = read_pcd(tmp_path / 'a.pcd'), read_pcd(tmp_path / 'b.pcd')
    assert a.shape == (500, 3) and b.shape == (500, 3)
    assert np.allclose(a, pts, atol=1e-3) and np.allclose(b, pts, atol=1e-5)


def test_read_ply_ascii(tmp_path):
    pts = [(0, 0, 0), (1, 1, 1), (2, 2, 2)]
    (tmp_path / 'c.ply').write_text('ply\nformat ascii 1.0\nelement vertex 3\nproperty float x\nproperty float y\nproperty float z\nend_header\n'
                                    + '\n'.join(' '.join(map(str, p)) for p in pts) + '\n')
    assert read_ply(tmp_path / 'c.ply').shape == (3, 3)


def test_voxel_downsample_reduces_points():
    pts = np.random.RandomState(1).rand(5000, 3) * 4
    ds = voxel_downsample(pts, 1.0)
    assert 1 < len(ds) <= 125                              # 4x4x4 体素
    assert len(voxel_downsample(pts, 0)) == 5000


def test_provider_roundtrip(tmp_path):
    prov = PointCloudProvider(tmp_path / 'pc')
    pts = np.random.RandomState(2).rand(3000, 3) * 20
    _write_pcd(tmp_path / 'm.pcd', pts)
    prov.save_upload('mapA', (tmp_path / 'm.pcd').read_bytes(), '.pcd')
    d = prov.downsampled('mapA', voxel=0.5, max_points=500)
    assert d['source_count'] == 3000 and d['count'] <= 500 and len(d['points'][0]) == 3
    assert prov.downsampled('nope') is None
