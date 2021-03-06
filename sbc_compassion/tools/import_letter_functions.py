# -*- coding: utf-8 -*-
##############################################################################
#
#    Copyright (C) 2014 Compassion CH (http://www.compassion.ch)
#    Releasing children from poverty in Jesus' name
#    @author: Loic Hausammann <loic_hausammann@hotmail.com>, Emanuel Cino
#
#    The licence is in the file __manifest__.py
#
##############################################################################
"""
Defines a few functions useful in ../models/import_letters_history.py
"""
import base64
import csv
import logging
import os
from collections import namedtuple
from io import BytesIO
from math import ceil
from time import time

from odoo import _, exceptions
from . import zxing_wrapper, zbar_wrapper, patternrecognition as pr, \
    checkboxreader as cbr, sniffpdf

_logger = logging.getLogger(__name__)

try:
    import numpy as np
    import cv2
    from pyPdf import PdfFileWriter, PdfFileReader
    from wand.image import Image as WandImage
except ImportError:
    _logger.warning('Please install numpy, cv2, pypdf and wand to use SBC '
                    'module')

##########################################################################
#                           GENERAL METHODS                              #
##########################################################################


def testline(env, line_vals, csv_file_ids, doc_name):
    """ Test each line and verify the data given by the csv.
    A csv file need to be written in the same way than
    sbc_compassion/tests/testdata/import/travis_file.csv

    :param env: Odoo environment
    :param dict line_vals: Dictionnary that will be used to create line
    :param Recordset('ir.attachment') csv_file_ids: CSV used to check
    :param str doc_name: Document name
    :returns: Error detected
    :rtype: str
    """
    # str that will be returned
    error = ''
    csv_data = []
    # creates a list containing all the csv data
    for csv_ in csv_file_ids:
        if os.path.splitext(csv_.name)[1] != '.csv':
            raise exceptions.UserError(
                _("File '{}' is not a csv file".format(csv_.name)))
        csv_string = base64.b64decode(csv_.datas).splitlines()
        csv_data += read_csv(csv_string)
    # check that the letter is present in the csv file and find it
    values = {}
    for csv_line in csv_data:
        if csv_line['name'] == doc_name:
            values = csv_line
            break
    else:
        # If no csv is given, don't check
        return error
    # Test that the template is the same as given in the csv
    template_csv = env.ref(
        'sbc_compassion.'+values['template_id'])
    if template_csv.id != line_vals['template_id']:
        error += 'template,\n'

    # Test that the barcode detection matches the result given in csv file
    barcode_tuple = namedtuple('BarcodeTuple', ['data'])
    barcode = barcode_tuple(values['barcode'])
    partner_detected, child_detected = decodeBarcode(env, barcode)
    # 1) we check the case where the barcode was not detected and was not
    # expected
    if (line_vals['child_id'] != child_detected or
            line_vals['partner_id'] != partner_detected):
        error += 'barcode,\n'

    # Test that languages detected are the same as given in the csv file
    letter_language = line_vals['test_letter_language']
    if letter_language != values['lang']:
        error += 'lang,\n'

    # remove the two last character (',\n')
    return error[:-2]


def update_stat_text(test_import):
    """ Update the text for the stats in test_import ("12/34 (35.3%)").
    Test is used in order to know if the number of passed test needs to be
    increased.

    :param test.import.letters.history test_import: Item to check
    :returns: Updated Item
    :rtype: test.import.letters.history
    """
    test = 0
    qr = 0
    tpl = 0
    lang = 0
    for line in test_import.test_import_line_ids:
        if line.error:
            test += 1
            if 'template' in line.error:
                tpl += 1
            if 'barcode' in line.error:
                qr += 1
            if 'lang' in line.error:
                lang += 1

    test = test_import.nber_test - test
    tpl = test_import.nber_test - tpl
    qr = test_import.nber_test - qr
    lang = test_import.nber_test - lang

    test_import.test_ok = write_text_test(test, test_import.nber_test)
    test_import.qr_ok = write_text_test(qr, test_import.nber_test)
    test_import.lang_ok = write_text_test(lang, test_import.nber_test)
    test_import.template_ok = write_text_test(tpl, test_import.nber_test)
    return test_import


def write_text_test(nber, tot):
    """ Write a text with the following form: "24/70 (34.28%)"

    :param int nber: Number of good case (24 in the example)
    :param int tot: Total number of case (70 in the example)
    :return: Text
    :rtype: str
    """
    ratio = float(nber) / float(tot)
    text = str(nber) + '/' + str(tot) + ' (' + str.format(
        "{0:.2f}", 100*ratio) + '%)'
    return text


def analyze_attachment(env, file_data, file_name, force_template, test=False):
    """
    Analyze attachment (PDF/TIFF) and save everything inside
    import_line_ids.
    The filename is given separately due to the name given by tempfile
    The test parameter is used to know if a real line needs to be created
    or a test.import.letter.line.

    Beware that image is converted to 300DPI, so templates should be defined
    with 300DPI images. Scans should also be with 300 DPI for better results.

    :param env env: Odoo variable env
    :param str file_data: Binary data of the image file to analyze
    :param str file_name: Name of the image file to analyze
    :param correspondence.template force_template: Template
    :param bool test: Test import or not

    :returns: Import Line values, IR Attachment values
    :rtype: list(dict), list(dict)
    """
    new_dpi = 100.0
    resize_ratio = new_dpi/300.0

    line_vals = list()
    document_vals = list()
    letter_datas = list()
    _logger.info("\tImport file : {}".format(file_name))

    inputpdf = PdfFileReader(BytesIO(file_data))
    tic = time()
    letter_indexes, imgs = _find_qrcodes(
        env, line_vals, inputpdf, new_dpi, test)
    _logger.info("\t{} letters found!".format(len(letter_indexes)-1 or 1))

    # Construct the datas for each detected letter: store as PDF
    if len(letter_indexes) > 1:
        last_index = 0
        for index in letter_indexes[1:]:
            output = PdfFileWriter()
            letter_data = BytesIO()
            for i in xrange(last_index, index):
                output.addPage(inputpdf.getPage(i))
            output.write(letter_data)
            letter_data.seek(0)
            letter_datas.append(letter_data.read())
            last_index = index
    else:
        letter_datas.append(file_data)

    # now try to find the layout for all splitted letters
    file_split = file_name.split('.')
    for i in range(len(letter_datas)):
        _logger.info(
            "\tAnalyzing template and language of letter {}/{}".format(
                i+1, len(letter_datas)))

        attach_name = file_split[0] + '-' + str(i) + '.pdf'
        document_vals.append({
            'name': attach_name,
            'datas': base64.b64encode(letter_datas[i]),
            'datas_fname': attach_name,
        })
        letter_vals = line_vals[i]
        if force_template:
            letter_vals['template_id'] = force_template.id
        else:
            # use pattern recognition to find the template
            _find_template(env, imgs[i], letter_vals, test, resize_ratio)
        if letter_vals['template_id'] != \
                env.ref('sbc_compassion.default_template').id:
            tic = time()
            _find_languages(env, imgs[i], letter_vals, test, resize_ratio)
            _logger.info(
                "\t\tLanguage analysis done in {:.3} sec.".format(time()-tic))

        else:
            _logger.info("\t\tAnalysis failed")
            letter_vals['letter_language_id'] = False
            if test:
                letter_vals.update({
                    'lang_preview': '',
                    'test_letter_language': ''
                })

    return line_vals, document_vals


def _find_qrcodes(env, line_vals, inputpdf, new_dpi, test):
    """
    Read the image and try to find the QR codes.
    The image should be currently saved as a png with the same name
    than :py:attr:`file_` (except for the extension).
    If QR Code is in wrong orientation, this method will return the given
    file.
    In case of test, the output dictonnary contains the image of the QR code
    too.

    :param env env: Odoo variable env
    :param dict line_vals: Dictionary that will hold values for import line
    :param inputpdf: PDFReader of the original pdf file
    :param bool test: Save the image of the QR code or not
    :returns: binary data of images, numpy arrays of pages to analyze further
    :rtype: list(str), list(np.array)
    """
    # Holds the indexes of the pages where a new letter is detected
    letter_indexes = list()
    page_imgs = list()

    previous_qrcode = ''
    _logger.info("\tThe imported PDF is made of {} pages.".format(
        inputpdf.numPages))
    for i in xrange(inputpdf.numPages):
        tic = time()
        output = PdfFileWriter()
        output.addPage(inputpdf.getPage(i))
        page_buffer = BytesIO()
        output.write(page_buffer)
        page_buffer.seek(0)

        # read the qrcode on the current page
        qrcode, img_path, cropped_img_path = _decode_page(
            env, page_buffer.read())

        if (qrcode and qrcode['data'] != previous_qrcode) or i == 0:
            previous_qrcode = qrcode and qrcode['data']
            letter_indexes.append(i)
            # we finally resize the image before returning it
            img = cv2.imread(img_path)
            f = new_dpi / 300.0
            img = cv2.resize(
                img, (0, 0), fx=f, fy=f, interpolation=cv2.INTER_CUBIC)
            page_imgs.append(img)

            partner_id, child_id = decodeBarcode(env, qrcode)
            page_preview = cv2.imencode('.jpg', img)
            preview_data = base64.b64encode(page_preview[1])

            values = {
                'partner_id': partner_id,
                'child_id': child_id,
                'letter_image_preview': preview_data
            }

            _logger.info(
                "\t\tPage {}/{} opened and QRCode analyzed in {:.2} "
                "sec".format(i + 1, inputpdf.numPages, time() - tic))

            if test:
                cropped = cv2.imread(cropped_img_path)
                cropped_preview = cv2.imencode('.jpg', cropped)
                cropped_data = base64.b64encode(cropped_preview[1])
                values['qr_preview'] = cropped_data
            line_vals.append(values)
        else:
            _logger.info(
                "\t\tPage {}/{} opened, no QRCode on this page. {:.2} "
                "sec".format(i + 1, inputpdf.numPages, time() - tic))

        os.remove(img_path)
        os.remove(cropped_img_path)
    letter_indexes.append(i+1)

    return letter_indexes, page_imgs


def _decode_page(env, page_data):
    """
    Read the image and try to find the QR codes.

    :param string page_data: Data of the PDF single page
    :returns: decoded qrcode, numpy array of page and test image data to show
              the detection
    :rtype: str, binary
    """
    tic = time()
    #  write a pdf file of the pdf data (which allows to perfom some
    # operations on it)
    tmp_url = sniffpdf.data2pdf(page_data)
    # Get the tree layout structure of the temporary PDF
    layouts = sniffpdf.get_layout(tmp_url)

    # The temporary PDF should only contain a single image
    if len(layouts) == 1 and sniffpdf.contains_a_single_image(layouts[0]):
        # extract jpg images from page_data en save them.
        img_url = sniffpdf.get_images(
            page_data, dst_folder=os.getcwd(), dst_name='page')
        img_url = img_url[0]
        img = cv2.imread(img_url)
        _logger.info("\t\tPDF opened with sniffpdf in {:.3} sec".format(
            time() - tic))
        # its time to remove the temporary PDF file
        os.remove(tmp_url)
    else:
        # It occurs that the page is not suitable to be simply opened as a
        # jpg image. This is why we have to convert it using a slower but
        # safer method

        # Slowly convert from vectorial to image data
        with WandImage(blob=page_data, resolution=300) as page_image:
            # Convert from image data to an array
            page_data = np.asarray(bytearray(page_image.make_blob('jpg')))
            # And we finally obtain our cv image
        img = cv2.imdecode(page_data, 1)  # Read in color
        if img is None:
            return None, None, None
        img_url = os.getcwd() + '/page.jpg'
        cv2.imwrite(img_url, img)

        _logger.info("\t\tPDF opened with wand.image in {:.3} sec".format(
            time() - tic))

    # We are now about to crop the img around the QRCode and save it on disk
    height = img.shape[0]
    width = img.shape[1]
    # get the crop coordinates
    left, right, top, bottom = _get_qr_crop(env, width, height)

    # Convert it to grayscale
    img = cv2.cvtColor(img, cv2.COLOR_RGB2GRAY)
    # And crop it around the qrcode
    cropped = img[top:bottom, left:right]
    cropped_url = os.getcwd() + '/page_cropped.jpg'
    cv2.imwrite(cropped_url, cropped)

    tic = time()

    decoder_lib = 'zbar'
    if decoder_lib == 'zxing':
        qrdata = zxing_wrapper.scan_qrcode(cropped_url)
        _logger.debug(
            "\t\tQRCode decoded using ZXing in {:.3} sec".format(time() -
                                                                 tic))
    elif decoder_lib == 'zbar':
        qrdata = zbar_wrapper.scan_qrcode(cropped_url)
        _logger.debug("\t\tQRCode decoded using ZBar in {:.3} sec".format(
            time()-tic))

    return qrdata, img_url, cropped_url


def _get_qr_crop(env, img_width, img_height):
    """ Computes the area to be croped for searching the QR code,
    given image height and width.

    :returns: left, right, top, bottom
    :rtype: int, int, int, int
    """
    left = img_width * float(
        env['ir.config_parameter'].get_param(
            'qrcode_x_min'))
    right = img_width * float(
        env['ir.config_parameter'].get_param(
            'qrcode_x_max'))
    top = img_height * float(
        env['ir.config_parameter'].get_param(
            'qrcode_y_min'))
    bottom = img_height * float(
        env['ir.config_parameter'].get_param(
            'qrcode_y_max'))
    return int(left), int(right), int(top), int(bottom)


def testDirectionQRcode(barcode):
    """ Test if the direction of the QR code is correct

    :param Barcode barcode: Data from zxing
    :return: True if the qrcode is in the right direction
    :rtype: bool
    """
    # vector between the two uppermost points (index 1 is
    # the one at the upper leftmost corner)
    top = np.array(barcode.points[2]) - np.array(barcode.points[1])
    left = np.array(barcode.points[0]) - np.array(barcode.points[1])
    diag = top + left
    if diag[0] > 0 < diag[1]:
        return True
    else:
        return False


def decodeBarcode(env, barcode):
    """ Split the barcode and return the id of the partner and the child.
    If the partner is not found, return None (same for the child).

    :param env: Odoo environment
    :param Barcode barcode:
    :return: partner.id, Child.id
    :rtype: int, int
    """
    partner_id = None
    child_id = None
    if barcode is not None and 'XX' in barcode['data']:
        barcode_split = barcode['data'].split('XX')
        if len(barcode_split) == 2:
            partner_ref, child_code = barcode_split
            child_ref_field = 'local_id'
            if len(child_code) == 9:
                # Old reference
                child_ref_field = 'code'
            child_id = env['compassion.child'].search(
                [(child_ref_field, '=', child_code)],
                order='id desc', limit=1).id
            partner_id = env['res.partner'].search(
                [('ref', '=', partner_ref),
                 ('has_sponsorships', '=', True)], limit=1).id
    return partner_id, child_id


def readEncode(img, format_img='png'):
    """
    Create a tempfile, write the image on it and return a base64 encoded
    string

    :param np.array img: Image
    :param str format_img: Format image
    :returns: Data (encoded in base64)
    :rtype: str
    """
    return base64.b64encode(cv2.imencode('.png', img)[1])


def _find_template(env, img, line_vals, test, resize_ratio):
    """
    Use pattern recognition to detect which template corresponds to img.

    :param env env: Odoo variable env
    :param img: Opencv Image to analyze
    :param dict line_vals: Dictonnary containing the data for a line
    :param bool test: Enable the test mode (will save some img)
    :returns: center position of detected pattern
    :rtype: layout
    """
    templates = env['correspondence.template'].search(
        [('pattern_image', '!=', False)])
    template, result_img = pr.find_template(
        img, templates, test=test, resize_ratio=resize_ratio)
    if test:
        result_preview = manyImages2OneImage(result_img, 1)
        line_vals['template_preview'] = result_preview

    if template is None:
        template = env.ref('sbc_compassion.default_template')

    line_vals['template_id'] = template.id


def _find_languages(env, img, line_vals, test, resize_ratio=1.0):
    """
    Crop a small part
    of the original picture around the position of each language
    check box.

    [TODO] Implement again something to transform coordinates and be
           tolerant when scans are not in same dpi as template or are
           misaligned.

    This analysis should be quite fast due to the small size of the
    pictures to analyze (should be a square of about 20-30 pixels large).

    Algorithm for finding the checked language is the following:
    1. Detect the box coordinates
    2. Compute Canny edges with two different approach and merge them
    3. Depending on the number of detected edges and a decision threshold
    we classe each box to True or False
    4. If 0 or more tha 1 box is checked, we don't return any result

    :param env env: Odoo variable env
    :param img: Image to analyze
    :param dict line_vals: Dictonnary containing the data for a line\
        (and the template)
    :param bool test: Enable the test mode (will save some img)
    :returns: None
    """
    line_vals['letter_language_id'] = False
    template = env['correspondence.template'].browse(
        line_vals['template_id'])
    if not template:
        return

    # Color for writing lang in test result image. The color order is BGR
    lang_color = (0, 97, 232)
    test_img = []
    h, w = img.shape[:2]

    checked = []
    checkbox_list = []
    for checkbox in template.checkbox_ids:
        a = int(checkbox.y_min * resize_ratio)
        b = int(checkbox.y_max * resize_ratio)
        c = int(checkbox.x_min * resize_ratio)
        d = int(checkbox.x_max * resize_ratio)
        if not (0 < a < b < h and 0 < c < d < w):
            continue
        checkbox_image = cbr.CheckboxReader(img[a:b+1, c:d+1])

        score = checkbox_image.compute_boxscore(boxsize=17)
        checkbox_list.append(checkbox)
        checked.append(checkbox_image.decision_threshold < score)

        if test:
            # Produce image of checkboxes to see the result of the crop
            pos = (int(checkbox.x_max-checkbox.x_min)/2,
                   int(checkbox.y_max-checkbox.y_min)/2)
            img_lang = np.copy(img[a:b+1, c:d+1])
            code_iso = checkbox.language_id.code_iso
            if code_iso and False:
                cv2.putText(img_lang, code_iso, pos,
                            cv2.FONT_HERSHEY_SIMPLEX, 1,
                            lang_color)
            test_img.append(img_lang)

    found = False
    checked_ind = [i for i, val in enumerate(checked) if val]
    langs = map(lambda ind: checkbox_list[ind], checked_ind)
    if len(langs) == 1:
        found = True
        lang = langs[0].language_id
        line_vals['letter_language_id'] = lang.id

    if test:
        test_data = manyImages2OneImage(test_img, 2)
        line_vals['lang_preview'] = test_data
        if found:
            vals = lang.code_iso
        else:
            vals = 'nope: ' + ', '.join(map(
                lambda l: l.language_id.code_iso, langs))
        line_vals['test_letter_language'] = vals


def manyImages2OneImage(test_img, col):
    """ Create an image from a list of image
    by creating a layout (with col columns)

    :param list test_img: list of array
    :param int col: Number of columns wanted
    """
    row = int(ceil(float(len(test_img)) / col))
    # first compute size of the final image
    height_img = 0
    width_img = 0
    for r in range(row):
        stop = min((r+1)*col, len(test_img))
        height_row, width_row = computeRowSize(test_img[r*col:stop])
        height_img += height_row
        if width_row > width_img:
            width_img = width_row
    # image that will be returned (initially black)
    # + col+1 is done for having at least 1 pixel between each
    # column (same for row)
    height_img += row + 1
    width_img += col + 1
    img = np.zeros((height_img, width_img, 3), np.uint8)
    top = 1
    for r in range(row):
        stop = min((r+1)*col, len(test_img))
        height_row, width_row = computeRowSize(test_img[r*col:stop])
        left = 1
        col_temp = col
        # last index computation a little bit different
        if r != row - 1:
            pad_left = (width_img - width_row) / (col_temp + 1)
        else:
            col_temp = len(test_img) - col*row
            if col_temp <= 0:
                col_temp += col
            pad_left = (width_img - width_row) / (col_temp + 1)
        for c in range(col_temp):
            h, w = test_img[r*col+c].shape[:2]
            left += pad_left
            pad_top = (height_row - h) / 2
            top_cell = top + pad_top
            img[top_cell:top_cell+h, left:left+w] = test_img[r*col+c]
            left += w
        top += height_row + 1
    return readEncode(img)


def computeRowSize(img):
    """
    Compute the size of a row in manyImages2OneImage

    :param list img: List of images
    :returns: height, width
    :rtype: int, int
    """
    height = 0
    width = 0
    for i in img:
        h, w = i.shape[:2]
        width += w
        if h > height:
            height = h
    return height, width


def check_file(name):
    """
    Check the name of a file.
    return 1 if it is a pdf and 0 otherwise.

    This function can be upgraded in order to include other
    format (1 for file, 2 for archive, 0 for not supported).
    In order to have a nice code, one should add the function
    is... when adding a new format

    :param str name: Name of the file to check
    :return: 1 if pdf, 2 if zip, 0 otherwise
    :rtype: int
    """
    if isPDF(name):
        return 1
    elif isZIP(name):
        return 2
    else:
        return 0


def isPDF(name):
    """
    Check the extension of the name

    :param string name: File name to check
    :returns: True if PDF, False otherwise
    :rtype: bool
    """
    ext = os.path.splitext(name)[1]
    return (ext.lower() == '.pdf')


def isTIFF(name):
    """
    Check the extension of the name

    :param string name: File name to check
    :returns: True if TIFF, False otherwise
    :rtype: bool
    """
    ext = os.path.splitext(name)[1]
    return (ext.lower() == '.tif' or ext.lower() == '.tiff')


def isPNG(name):
    """
    Check the extension of the name

    :param string name: File name to check
    :returns: True if PNG, False otherwise
    :rtype: bool
    """
    ext = os.path.splitext(name)[1]
    return (ext.lower() == '.png')


def isZIP(name):
    """
    Check the extension of the name

    :param string name: File name to check
    :returns: True if ZIP, False otherwise
    :rtype: bool
    """
    ext = os.path.splitext(name)[1]
    return (ext.lower() == '.zip')


def read_csv(csv_file):
    """
    Reads a .csv file and returns its content as a list of dicts.

    Just like in csv.reader, csv_file can be any object which supports the
    iterator protocol and returns a string each time its next method is called.
    File objects and list objects are both suitable.
    """
    file_list = []
    reader = csv.reader(csv_file)
    header = reader.next()
    for row in reader:
        values = {}
        for key, value in zip(header, row):
            values[key] = value
        file_list.append(values)
    return file_list
