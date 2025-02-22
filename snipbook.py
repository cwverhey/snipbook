#!/usr/bin/env python3

import os
import argparse
import json
import tempfile

# pip install numpy scipy pillow fpdf
import numpy as np
from scipy.ndimage import label
from PIL import Image
from fpdf import FPDF


PAGE_SIZES = {
    # page sizes in mm
    'A4': [210, 297],
    'A5': [148, 210],
    'letter': [215.9, 279.4],
    'legal': [215.9, 355.6]
}
MERGE_DEFAULT_PILLOW_PARAMS = {
    'png': {'optimize': True},
    'jpeg': {'quality': 50}
}


def main():
    parser = argparse.ArgumentParser(description="Cut images and merge them into a PDF")
    subparsers = parser.add_subparsers(dest='command')

    meld_parser = subparsers.add_parser('meld', help="Step 1: Meld multiple images into 1 image, to determine ROIs")
    meld_parser.add_argument('files', nargs='+', help="input image files")
    meld_parser.add_argument('--output', '-o', required=True, help="output PNG filename")
    meld_parser.add_argument('--method', '-m', choices=['min', 'max'], default='min', help="melding method (default: %(default)s)")
    
    snip_parser = subparsers.add_parser('snip', help="Step 2: Snip ROIs from images")
    snip_parser.add_argument('files', nargs='+', help="input image files")
    snip_parser.add_argument('--roi', '-r', required=True, help="JSON list of areas [[left, top, right, bottom], ...], or file (PNG with transparent areas as ROIs)")
    snip_parser.add_argument('--outputdir', '-o', required=True, help="output directory")
    snip_parser.add_argument('--crop', '-c', default='no', help="autocrop color; options: 'no', '#xxxxxx' (hex RGB), 'auto' (topleft pixel color) (default: %(default)s)")
    snip_parser.add_argument('--tolerance', '-t', default=10, type=int, help="tolerance for autocrop color (in %%; default: %(default)s)")
    snip_parser.add_argument('--format', '-f', type=str.lower, default='png', choices=['png', 'jpeg'], help="output format (default: %(default)s)")

    merge_parser = subparsers.add_parser('merge', help="Step 3: Merge snipped ROIs into a single PDF")
    merge_parser.add_argument('files', nargs='+', help="input image files")
    merge_parser.add_argument('--margin', '-m', default=20, help="margin around image (in mm; default: %(default)s)")
    merge_parser.add_argument('--size', '-s', default='auto', help=f"output page size; options: auto (= largest input image + margins), {', '.join(PAGE_SIZES.keys())}, \"[123,456]\" (in mm, w×h) (default: %(default)s)")
    merge_parser.add_argument('--dpi', '-d', default=72, type=int, help="DPI for the image (only with size=auto, otherwise DPI is set automatically; default: %(default)s)")
    merge_parser.add_argument('--expand', '-e', nargs='*', type=int, help="expand image on this page to the full page, without margin")
    merge_parser.add_argument('--output', '-o', required=True, help="output PDF filename")
    merge_parser.add_argument('--format', '-f', choices=['jpeg','png'], default='jpeg', type=str.lower, help="image format (default: %(default)s)")
    merge_parser.add_argument('--pillow', '-p', default='auto', help=f"pillow image saving parameters, in JSON. See https://pillow.readthedocs.io/en/stable/handbook/image-file-formats.html. (auto = {'; '.join([f'{k}: \'{json.dumps(v)}\'' for k,v in MERGE_DEFAULT_PILLOW_PARAMS.items()])}). (default: %(default)s)")
    
    args = parser.parse_args()
    if args.command == 'meld':
        meld(args)
    elif args.command == 'snip':
        snip(args)
    elif args.command == 'merge':
        merge(args)
    else:
        parser.print_help()


def meld(args):
    with open(args.output, 'xb') as file:
        print(f'Melding {len(args.files)} images using method "{args.method}"')
        combined = combine_multiple(args.files, args.method)
        print(f'Saving to "{args.output}"')
        combined.save(file, format='png')


def snip(args):
    
    # load ROIs from JSON string or file
    try:
        roi = json.loads(args.roi)
        print(f"ROIs: ", end='')
    except json.decoder.JSONDecodeError:
        print(f"Loading ROIs from {args.roi}: ", end='')
        roi_img = np.array(Image.open(args.roi))
        full_alpha = roi_img[:, :, 3] == 0
        roi = find_contiguous_rectangles(full_alpha)
    print(f'{len(roi)} regions: {roi}')

    # create output dir
    print(f"Creating output directory '{args.outputdir}'")
    os.makedirs(args.outputdir)

    # iterate over files and roi; snip sections
    print(f"Snipping {len(roi)} regions from {len(args.files)} images, crop {args.crop} ±{args.tolerance}%")
    i = 0
    for f in args.files:
        img = Image.open(f)

        for (ri, r) in enumerate(roi):
            i += 1
            outfile = f"{os.path.splitext(os.path.basename(f))[0]}-{ri+1}.{args.format}"
            print(f"{i}/{len(roi) * len(args.files)}: {outfile}")
            crop(img, r, args.crop, args.tolerance).save(os.path.join(args.outputdir, outfile))


def merge(args):

    if os.path.exists(args.output):
        raise FileExistsError(f"The file '{args.output}' already exists")  # FPDF happily overwrites :/

    print(f"Loading dimensions (w×h):")
    max_img_dim = [0,0]
    for f in args.files:
        img = Image.open(f)
        max_img_dim = [ max(x,y) for x,y in zip(max_img_dim, img.size) ]
    print(f"\tLargest input: {max_img_dim} px")

    if args.size.lower() == 'auto':
        # scale is fixed, determine page dimensions from largest image dimensions + margin
        scale = args.dpi / 25.4  # px/mm
        page_dim = [ 2*args.margin + i/scale for i in max_img_dim]  # mm
    else:
        # page size is fixed, determine scale from largest image: maximum possible size that fits on (page size - margin)
        page_dim = page_size(args.size)
        page_sans_margin = [ d - 2*args.margin for d in page_dim ]
        scale = max([ i/p for i, p in zip(max_img_dim, page_sans_margin) ])

    print(f"\tPage: {[round(p, 2) for p in page_dim]} mm")
    print(f"\tScale: {round(scale,2)} px/mm = {round(scale*25.4,2)} dpi")

    # create PDF
    pillow_options = merge_pillow_options(args.pillow, args.format)
    print(f"Merging {len(args.files)} images into PDF with pillow options {pillow_options}:")
    pdf = FPDF(unit = 'mm', format = page_dim)
    pdf.set_auto_page_break(auto=True, margin=0)

    for i, f in enumerate(args.files):
        print(f"{i+1}/{len(args.files)}: {f}")

        img = Image.open(f).convert('RGB')
        if i+1 in args.expand:
            img_scale = max([ i/p for i, p in zip(img.size, page_dim) ])
        else:
            img_scale = scale
        img_dim = [ i_s / img_scale for i_s in img.size]
        img_offset = [ (p-i)/2 for i, p in zip(img_dim, page_dim)]

        pdf.add_page()

        with tempfile.NamedTemporaryFile(suffix=f'.{args.format}') as temp_img_file:
            img.save(temp_img_file.name, **pillow_options)
            pdf.image(temp_img_file.name, x=img_offset[0], y=img_offset[1], w=img_dim[0], h=img_dim[1])

    print(f"Writing to '{args.output}'")
    pdf.output(args.output)


def combine_multiple(images, method='min'):

    assert len(images), "at least 1 image should be provided"

    combined = images[0]
    for i, img in enumerate(images[1:]):
        print(f'{i+1}/{len(images)-1}', end='\r')
        if isinstance(combined, str):
            combined = Image.open(combined)
        if isinstance(img, str):
            img = Image.open(img)
        combined = combine_two(combined, img, method)
    print(' '+'  '*len(images), end='\r')
    return combined
    

def combine_two(img1, img2, method):
    
    assert method in ['min', 'max'], "Method must be 'min' or 'max'."
    assert isinstance(img1, Image.Image), "img1 must be a PIL Image."
    assert isinstance(img2, Image.Image), "img2 must be a PIL Image."
    
    max_dims = max(img1.width, img2.width), max(img1.height, img2.height)
    img1 = np.array(resize_and_center(img1, max_dims))
    img2 = np.array(resize_and_center(img2, max_dims))

    if method == 'min':
        combined = np.minimum(img1, img2)
    elif method == 'max':
        combined = np.maximum(img1, img2)

    return Image.fromarray(combined.astype(np.uint8))


def resize_and_center(img, target_size):
    if img.size == target_size:
        return img
    new_img = Image.new("RGBA", target_size, (0, 0, 0, 0))  # fully transparent
    new_img.paste(img, ((target_size[0] - img.size[0]) // 2, (target_size[1] - img.size[1]) // 2))
    return new_img


def find_contiguous_rectangles(nparr):
    rectangles = []
    labeled_array, num_features = label(nparr)
    for i in range(1, num_features+1):
        rows, cols = np.where(labeled_array == i)
        rectangles.append([ int(cols.min()), int(rows.min()), int(cols.max()), int(rows.max()) ])
    rectangles = sorted(rectangles, key=lambda x: 2*x[0] + x[1])
    return rectangles


def hex_to_rgb(hex):
    hex = hex.lstrip("#")
    if len(hex) != 6:
        raise ValueError("Hex color must be 7 characters long incl. #")
    return tuple(int(hex[i:i+2], 16) for i in (0, 2, 4))


def crop(img, region, autocrop_color, autocrop_tolerance):
    '''
    Crop and then autocrop an image

    @param img: PIL Image
    @param region: [left, top, right, bottom]
    @param autocrop_color: 'NO' / 'AUTO' / '#000FFF'
    @param autocrop_tolerance: 0-100
    @returns PIL Image
    '''

    autocrop_tolerance = (autocrop_tolerance / 100) * 255  # from % to raw 8-bit value

    if autocrop_color.upper() == 'AUTO':
        autocrop_color = np.array(img.getpixel((0, 0)), dtype=np.int16)
    elif autocrop_color.upper() == 'NO':
        autocrop_color = False
    else:
        autocrop_color = np.array(hex_to_rgb(autocrop_color), dtype=np.int16)
    
    # crop region
    img_cropped = img.crop(region)

    # autocrop
    if autocrop_color is not False:

        # convert to NumPy array
        np_cropped = np.array(img_cropped, dtype=np.int16)  # int16 instead of uint8 because subtracting the autocrop_color would wrap around with unsigned

        # create mask of pixels within tolerance
        mask = np.all(np.abs(np_cropped[:, :, :3] - autocrop_color[:3]) <= autocrop_tolerance, axis=-1)

        # find bounding box of remaining pixels
        coords = np.argwhere(~mask)
        if coords.size:
            top, left = coords.min(axis=0)
            bottom, right = coords.max(axis=0) + 1
            img_cropped = img_cropped.crop((left, top, right, bottom))

    return img_cropped


def page_size(str):
    try:
        return json.loads(str)
    except json.decoder.JSONDecodeError:
        return PAGE_SIZES[str]


def merge_pillow_options(str, format):
    vals = {'format': format}
    if str.lower() == 'auto':
        vals.update(MERGE_DEFAULT_PILLOW_PARAMS.get(format, []))
    else:
        vals.update(json.loads(str))
    return vals

if __name__ == '__main__':
    main()