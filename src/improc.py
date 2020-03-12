import functools
import logging
import subprocess

import PIL
import cv2
import imageio
import jpeg4py
import numba
import numpy as np
import pycocotools.mask


def encode_mask(mask):
    return pycocotools.mask.encode(np.asfortranarray(mask.astype(np.uint8)))


def decode_mask(encoded_mask):
    return pycocotools.mask.decode(encoded_mask)


def resize_by_factor(im, factor, interp=None):
    """Returns a copy of `im` resized by `factor`, using bilinear interp for up and area interp
    for downscaling.
    """
    new_size = rounded_int_tuple([im.shape[1] * factor, im.shape[0] * factor])
    if interp is None:
        interp = cv2.INTER_LINEAR if factor > 1.0 else cv2.INTER_AREA
    return cv2.resize(im, new_size, fx=factor, fy=factor, interpolation=interp)


def num_frames_of_video(path):
    with imageio.get_reader(path) as vid:
        return vid.get_meta_data()['nframes']


def figure_to_image(fig):
    img = np.fromstring(fig.canvas.tostring_rgb(), dtype=np.uint8, sep='')
    width, height = fig.canvas.get_width_height()
    return img.reshape([height, width, 3])


def remove_small_components(mask, min_size, inplace=False, connectivity=8):
    n_labels, labels, stats, centroids = cv2.connectedComponentsWithStats(
        mask, connectivity, cv2.CV_32S)
    is_small = stats[:, -1] < min_size
    ids_small = np.squeeze(np.argwhere(is_small), axis=-1)

    if not inplace:
        mask = mask.copy()

    for i in ids_small:
        mask[labels == i] = 0

    return mask


@functools.lru_cache()
def get_structuring_element(shape, ksize, anchor=None):
    if not isinstance(ksize, tuple):
        ksize = (ksize, ksize)
    return cv2.getStructuringElement(shape, ksize, anchor)


def largest_connected_component(mask):
    mask = mask.astype(np.uint8)
    n_labels, labels, stats, centroids = cv2.connectedComponentsWithStats(mask, 4, cv2.CV_32S)
    areas = stats[1:, -1]
    if len(areas) < 1:
        return mask, np.array([0, 0, 0, 0])

    largest_area_label = 1 + np.argsort(areas)[-1]
    obj_mask = np.uint8(labels == largest_area_label)
    obj_box = stats[largest_area_label, :4]

    return obj_mask, np.array(obj_box)


def rounded_int_tuple(p):
    return tuple(np.round(p).astype(int))


def rectangle(im, pt1, pt2, *args, **kwargs):
    cv2.rectangle(im, rounded_int_tuple(pt1), rounded_int_tuple(pt2), *args, **kwargs)


def image_extents(filepath):
    """Returns the image (width, height) as a numpy array, without loading the pixel data."""

    with PIL.Image.open(filepath) as im:
        return np.asarray(im.size)


def normalize_01(im):
    result = np.empty_like(im, dtype=np.float32)
    # cv2.normalize(im, dst=result, alpha=0, beta=1, norm_type=cv2.NORM_MINMAX, dtype=cv2.CV_32F)
    cv2.divide(im, (255, 255, 255, 255), dst=result, dtype=cv2.CV_32F)
    np.clip(result, 0, 1, out=result)
    return result


def imread_jpeg_fast(path):
    if isinstance(path, bytes):
        path = path.decode('utf8')
    elif isinstance(path, np.str):
        path = str(path)

    try:
        return jpeg4py.JPEG(path).decode()
    except jpeg4py.JPEGRuntimeError:
        logging.error(f'Could not load image at {path}, JPEG error.')
        raise


def paste_over(im_src, im_dst, alpha, center, inplace=False):
    """Pastes `im_src` onto `im_dst` at a specified position, with alpha blending.

    The resulting image has the same shape as `im_dst` but contains `im_src`
    (perhaps only partially, if it's put near the border).
    Locations outside the bounds of `im_dst` are handled as expected
    (only a part or none of `im_src` becomes visible).

    Args:
        im_src: The image to be pasted onto `im_dst`. Its size can be arbitrary.
        im_dst: The target image.
        alpha: A float (0.0-1.0) image of the same size as `im_src` controlling the alpha blending
            at each pixel. Large values mean more visibility for `im_src`.
        center: coordinates in `im_dst` where the center of `im_src` should be placed.

    Returns:
        An image of the same shape as `im_dst`, with `im_src` pasted onto it.
    """

    width_height_src = np.asarray([im_src.shape[1], im_src.shape[0]])
    width_height_dst = np.asarray([im_dst.shape[1], im_dst.shape[0]])

    center = np.round(center).astype(np.int32)
    ideal_start_dst = center - width_height_src // 2
    ideal_end_dst = ideal_start_dst + width_height_src

    start_dst = np.clip(ideal_start_dst, 0, width_height_dst)
    end_dst = np.clip(ideal_end_dst, 0, width_height_dst)

    if inplace:
        result = im_dst
    else:
        result = im_dst.copy()

    region_dst = result[start_dst[1]:end_dst[1], start_dst[0]:end_dst[0]]

    start_src = start_dst - ideal_start_dst
    end_src = width_height_src + (end_dst - ideal_end_dst)

    if alpha is None:
        alpha = np.ones(im_src.shape[:2], dtype=np.float32)

    if alpha.ndim < im_src.ndim:
        alpha = np.expand_dims(alpha, -1)
    alpha = alpha[start_src[1]:end_src[1], start_src[0]:end_src[0]]

    region_src = im_src[start_src[1]:end_src[1], start_src[0]:end_src[0]]

    result[start_dst[1]:end_dst[1], start_dst[0]:end_dst[0]] = (
            alpha * region_src + (1 - alpha) * region_dst)
    return result


def adjust_gamma(image, gamma, inplace=False):
    if inplace:
        cv2.LUT(image, get_gamma_lookup_table(gamma), dst=image)
        return image

    return cv2.LUT(image, get_gamma_lookup_table(gamma))


@functools.lru_cache()
def get_gamma_lookup_table(gamma):
    return (np.linspace(0, 1, 256) ** gamma * 255).astype(np.uint8)


def blend_image(im1, im2, im2_weight):
    if im2_weight.ndim == im1.ndim - 1:
        im2_weight = im2_weight[..., np.newaxis]

    return blend_image_numba(
        im1.astype(np.float32),
        im2.astype(np.float32),
        im2_weight.astype(np.float32)).astype(im1.dtype)


@numba.jit(nopython=True)
def blend_image_numba(im1, im2, im2_weight):
    return im1 * (1 - im2_weight) + im2 * im2_weight


def is_image_readable(path):
    return subprocess.call(
        ['/usr/bin/identify', path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) == 0


def white_balance(img, a=None, b=None):
    result = cv2.cvtColor(img, cv2.COLOR_RGB2LAB)
    avg_a = a if a is not None else np.mean(result[..., 1])
    avg_b = b if b is not None else np.mean(result[..., 2])
    result[..., 1] = result[..., 1] - ((avg_a - 128) * (result[..., 0] / 255.0) * 1.1)
    result[..., 2] = result[..., 2] - ((avg_b - 128) * (result[..., 0] / 255.0) * 1.1)
    result = cv2.cvtColor(result, cv2.COLOR_LAB2RGB)
    return result
