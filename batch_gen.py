"""
Includes:
* A batch generator for SSD model training and inference which can perform online data augmentation
* An offline image processor that saves processed images and adjusted labels to disk

Copyright (C) 2017 Pierluigi Ferrari

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with this program.  If not, see <http://www.gnu.org/licenses/>.
"""

import csv
import os
import random
from copy import deepcopy

import cv2
import numpy as np
from PIL import Image
from sklearn.utils import shuffle


# Image processing functions used by the generator to perform the following image manipulations:
# - Translation
# - Horizontal flip
# - Scaling
# - Brightness change
# - Histogram contrast equalization

def translate(image, horizontal=(0, 40), vertical=(0, 10)):
    """
    Randomly translate the input image horizontally and vertically.

    Arguments:
        image (array-like): The image to be translated.
        horizontal (int tuple, optional): A 2-tuple `(min, max)` with the minimum
            and maximum horizontal translation. A random translation value will
            be picked from a uniform distribution over [min, max].
        vertical (int tuple, optional): Analog to `horizontal`.

    Returns:
        The translated image and the horizontal and vertical shift values.
    """
    rows, cols, ch = image.shape

    x = np.random.randint(horizontal[0], horizontal[1] + 1)
    y = np.random.randint(vertical[0], vertical[1] + 1)
    x_shift = random.choice([-x, x])
    y_shift = random.choice([-y, y])

    trans_mat = np.float32([[1, 0, x_shift], [0, 1, y_shift]])
    return cv2.warpAffine(image, trans_mat, (cols, rows)), x_shift, y_shift


def flip(image, orientation='horizontal'):
    """
    Flip the input image horizontally or vertically.
    """
    if orientation == 'horizontal':
        return cv2.flip(image, 1)
    else:
        return cv2.flip(image, 0)


def scaling(image, min_scale=0.9, max_scale=1.1):
    """
    Scale the input image by a random factor picked from a uniform distribution
    over [min, max].

    Returns:
        The scaled image, the associated warp matrix, and the scaling value.
    """

    rows, cols, ch = image.shape

    # Randomly select a scaling factor from the range passed.
    scale = np.random.uniform(min_scale, max_scale)

    scale_mat = cv2.getRotationMatrix2D((cols / 2, rows / 2), 0, scale)
    return cv2.warpAffine(image, scale_mat, (cols, rows)), scale_mat, scale


def brightness_scale(image, min_scale=0.5, max_scale=2.0):
    """
    Randomly change the brightness of the input image.

    Protected against overflow.
    """
    hsv = cv2.cvtColor(image, cv2.COLOR_RGB2HSV)

    random_br = np.random.uniform(min_scale, max_scale)

    # To protect against overflow: Calculate a mask for all pixels
    # where adjustment of the brightness would exceed the maximum
    # brightness value and set the value to the maximum at those pixels.
    mask = hsv[:, :, 2] * random_br > 255
    # noinspection PyTypeChecker
    v_channel = np.where(mask, 255, hsv[:, :, 2] * random_br)
    hsv[:, :, 2] = v_channel

    return cv2.cvtColor(hsv, cv2.COLOR_HSV2RGB)


def histogram_eq(image):
    """
    Perform histogram equalization on the input image.

    See https://en.wikipedia.org/wiki/Histogram_equalization.
    """

    image1 = np.copy(image)

    image1[:, :, 0] = cv2.equalizeHist(image1[:, :, 0])
    image1[:, :, 1] = cv2.equalizeHist(image1[:, :, 1])
    image1[:, :, 2] = cv2.equalizeHist(image1[:, :, 2])

    return image1


class BatchGenerator:
    """
    A generator to generate batches of samples and corresponding labels indefinitely.

    The labels are read from a CSV file.

    Shuffles the dataset consistently after each complete pass.

    Can perform image transformations for data conversion and data augmentation,
    for details please refer to the documentation of the `generate()` method.
    """

    def __init__(self, labels_path=None, input_format=None, include_classes=None):

        # These are the variables we always need
        box_output_format = ['class_id', 'xmin', 'xmax', 'ymin', 'ymax']
        # self.images_path = images_path
        self.class_map = {v:k for k, v in enumerate(include_classes)}
        self.include_classes = include_classes
        self.box_output_format = box_output_format

        # These are the variables that we only need if we want to use parse_csv()
        self.labels_path = None
        self.input_format = None

        # These are the variables that we only need if we want to use parse_xml()
        self.annotations_path = None
        self.image_set_path = None
        self.image_set = None
        self.classes = None

        # The two variables below store the output from the parsers. This is the input for the generate() method
        # `self.filenames` is a list containing all file names of the image samples. Note that it does not contain
        # the actual image files themselves.
        self.filenames = []  # All unique image filenames will go here
        # `self.labels` is a list containing one 2D Numpy array per image. For an image with `k` ground truth
        # bounding boxes, the respective 2D array has `k` rows, each row containing `(xmin, xmax, ymin, ymax,
        # class_id)` for the respective bounding box.
        self.labels = []
        # Each entry here will contain a 2D Numpy array with all the ground truth boxes for a given image
        self.parse_csv( labels_path, input_format)

    def parse_csv(self,
                  labels_path=None,
                  input_format=None):
        """
        Arguments:
            labels_path (str, optional): The filepath to a CSV file that contains one ground truth bounding box per line
                and each line contains the following six items: image file name, class ID, xmin, xmax, ymin, ymax.
                The six items do not have to be in a specific order, but they must be the first six columns of
                each line. The order of these items in the CSV file must be specified in `input_format`.
                The class ID is an integer greater than zero. Class ID 0 is reserved for the background class.
                `xmin` and `xmax` are the left-most and right-most absolute horizontal coordinates of the box,
                `ymin` and `ymax` are the top-most and bottom-most absolute vertical coordinates of the box.
                The image name is expected to be just the name of the image file without the directory path
                at which the image is located. Defaults to `None`.
            input_format (list, optional): A list of six strings representing the order of the six items
                image file name, class ID, xmin, xmax, ymin, ymax in the input CSV file. The expected strings
                are 'image_name', 'xmin', 'xmax', 'ymin', 'ymax', 'class_id'. Defaults to `None`.
            ret (bool, optional): Whether or not the image filenames and labels are to be returned.
                Defaults to `False`.

        Returns:
            None by default, optionally the image filenames and labels.
        """

        # If we get arguments in this call, set them
        if labels_path is not None:
            self.labels_path = labels_path
        if input_format is not None:
            self.input_format = input_format

        # Before we begin, make sure that we have a labels_path and an input_format
        if self.labels_path is None or self.input_format is None:
            raise ValueError(
                "`labels_path` and/or `input_format` have not been set yet. You need to pass them as arguments.")

        # Erase data that might have been parsed before
        self.filenames = []
        self.labels = []

        # First, just read in the CSV file lines and sort them.

        data = []

        with open(self.labels_path, newline='') as csvfile:
            csv_reader = csv.reader(csvfile, delimiter=',')
            k = 0
            for i in csv_reader:  # For every line (i.e for every bounding box) in the CSV file...
                if k == 0:  # Skip the header row
                    k += 1
                    continue
                else:
                    if self.include_classes is None or int(i[self.input_format.index(
                            'class_id')].strip()) in self.include_classes:
                        # If the class_id is among the classes that are to be included in the dataset...
                        obj = [i[self.input_format.index('image_name')].strip()]
                        # Store the box class and coordinates here

                        for item in self.box_output_format:  # For each item in the output format...
                            val = int(i[self.input_format.index(item)].strip())
                            if item == 'class_id':
                                obj.append(self.class_map[val])
                            else:
                                obj.append(val)
                        data.append(obj)

        data = sorted(data)  # The data needs to be sorted, otherwise the next step won't give the correct result

        # Now that we've made sure that the data is sorted by file names,
        # we can compile the actual samples and labels lists

        current_file = ''  # The current image for which we're collecting the ground truth boxes
        current_labels = []  # The list where we collect all ground truth boxes for a given image
        for idx, i in enumerate(data):
            if current_file == '':  # If this is the first image file
                current_file = i[0]
                current_labels.append(i[1:])
                if len(data) == 1:  # If there is only one box in the CVS file
                    self.labels.append(np.stack(current_labels, axis=0))
                    self.filenames.append(current_file)
            else:
                if i[0] == current_file:
                    # If this box (i.e. this line of the CSV file) belongs to the current image file
                    current_labels.append(i[1:])
                    if idx == len(data) - 1:  # If this is the last line of the CSV file
                        self.labels.append(np.stack(current_labels, axis=0))
                        self.filenames.append(current_file)
                else:  # If this box belongs to a new image file
                    self.labels.append(np.stack(current_labels, axis=0))
                    self.filenames.append(current_file)
                    current_labels = []
                    current_file = i[0]
                    current_labels.append(i[1:])
        #
        # if ret:  # In case we want to return these
        #     return self.filenames, self.labels

        self.count = len(self.filenames)

    def generate(self,
                 batch_size=32,
                 train=True,
                 ssd_box_encoder=None,
                 equalize=False,
                 brightness=None,
                 flip=None,
                 scale=None,
                 random_crop=None,
                 crop=None,
                 resize=None,
                 gray=False,
                 limit_boxes=True,
                 include_thresh=0.3):
        """
        Generate batches of samples and corresponding labels indefinitely from
        lists of filenames and labels.

        Returns two numpy arrays, one containing the next `batch_size` samples
        from `filenames`, the other containing the corresponding labels from
        `labels`.

        Shuffles `filenames` and `labels` consistently after each complete pass.

        Can perform image transformations for data conversion and data augmentation.
        `resize`, `gray`, and `equalize` are image conversion tools and should be
        used consistently during training and inference. The remaining transformations
        serve for data augmentation. Each data augmentation process can set its own
        independent application probability. The transformations are performed
        in the order of their arguments, i.e. equalization is performed first,
        gray scale conversion is performed last.

        `prob` works the same way in all arguments in which it appears. It must be a float in [0,1]
        and determines the probability that the respective transform is applied to any given image.

        All conversions and transforms default to `False`.

        Arguments:
            batch_size (int, optional): The size of the batches to be generated. Defaults to 32.
            train (bool, optional): Whether or not the generator is used in training mode. If `True`, then the labels
                will be transformed into the format that the SSD cost function requires. Otherwise,
                the output format of the labels is identical to the input format. Defaults to `True`.
            ssd_box_encoder (SSDBoxEncoder, optional): Only required if `train = True`. An SSDBoxEncoder object
                to encode the ground truth labels to the required format for training an SSD model.
            equalize (bool, optional): If `True`, performs histogram equalization on the images.
                This can improve contrast and lead the improved model performance.
            brightness (tuple, optional): `False` or a tuple containing three floats, `(min, max, prob)`.
                Scales the brightness of the image by a factor randomly picked from a uniform
                distribution in the boundaries of `[min, max]`. Both min and max must be >=0.
            flip (float, optional): `False` or a float in [0,1], see `prob` above. Flip the image horizontally.
                The respective box coordinates are adjusted accordingly.
            scale (tuple, optional): `False` or a tuple containing three floats, `(min, max, prob)`.
                Scales the image by a factor randomly picked from a uniform distribution in the boundaries
                of `[min, max]`. Both min and max must be >=0.
            random_crop (tuple, optional): `False` or a tuple of four integers,
                `(height, width, min_1_object, max_#_trials)`,
                where `height` and `width` are the height and width of the patch that is to be cropped out at a random
                position in the input image. Note that `height` and `width` can be arbitrary - they are allowed to be
                larger
                than the image height and width, in which case the original image will be randomly placed on a black
                background
                canvas of size `(height, width)`. `min_1_object` is either 0 or 1. If 1, there must be at least one
                detectable
                object remaining in the image for the crop to be valid, and if 0, crops with no detectable objects
                left in the
                image patch are allowed. `max_#_trials` is only relevant if `min_1_object == 1` and sets the maximum
                number
                of attempts to get a valid crop. If no valid crop was obtained within this maximum number of attempts,
                the respective image will be removed from the batch without replacement (i.e. for each removed image,
                the batch
                will be one sample smaller). Defaults to `False`.
            crop (tuple, optional): `False` or a tuple of four integers,
                `(crop_top, crop_bottom, crop_left, crop_right)`,
                with the number of pixels to crop off of each side of the images.
                The targets are adjusted accordingly. Note: Cropping happens before resizing.
            resize (tuple, optional): `False` or a tuple of 2 integers for the desired output
                size of the images in pixels. The expected format is `(width, height)`.
                The box coordinates are adjusted accordingly. Note: Resizing happens after cropping.
            gray (bool, optional): If `True`, converts the images to gray scale.
            limit_boxes (bool, optional): If `True`, limits box coordinates to stay within image boundaries
                post any transformation. This should always be set to `True`, even if you set `include_thresh`
                to 0. I don't even know why I made this an option. If this is set to `False`, you could
                end up with some boxes that lie entirely outside the image boundaries after a given transformation
                and such boxes would of course not make any sense and have a strongly adverse effect on the learning.
            include_thresh (float, optional): Only relevant if `limit_boxes` is `True`. Determines the minimum
                fraction of the area of a ground truth box that must be left after limiting in order for the box
                to still be included in the batch data. If set to 0, all boxes are kept except those which lie
                entirely outside of the image boundaries after limiting. If set to 1, only boxes that did not
                need to be limited at all are kept. Defaults to 0.3.


        Yields:
            The next batch as a tuple containing a Numpy array that contains the images and a python list
            that contains the corresponding labels for each image as 2D Numpy arrays. The output format
            of the labels is according to the `box_output_format` that was specified in the constructor.

        """

        self.filenames, self.labels = shuffle(self.filenames, self.labels)  # Shuffle the data before we begin
        current = 0

        # Find out the indices of the box coordinates in the label data
        xmin = self.box_output_format.index('xmin')
        xmax = self.box_output_format.index('xmax')
        ymin = self.box_output_format.index('ymin')
        ymax = self.box_output_format.index('ymax')

        while True:

            batch__x, batch_y = [], []

            # Shuffle the data after each complete pass
            if current >= len(self.filenames):
                self.filenames, self.labels = shuffle(self.filenames, self.labels)
                current = 0

            for filename in self.filenames[current:current + batch_size]:
                with Image.open(filename) as img:
                    batch__x.append(np.array(img))
            batch_y = deepcopy(self.labels[current:current + batch_size])

            this_filenames = self.filenames[current:current + batch_size]

            current += batch_size

            # At this point we're done producing the batch. Now perform some
            # optional image transformations:

            batch_items_to_remove = []
            # In case we need to remove any images from the batch because of failed random cropping, store their
            # indices in this list

            for i in range(len(batch__x)):

                img_height, img_width, ch = batch__x[i].shape
                batch_y[i] = np.array(batch_y[i])
                # Convert labels into an array (in case it isn't one already), otherwise the indexing below breaks

                if equalize:
                    batch__x[i] = histogram_eq(batch__x[i])

                if brightness:
                    p = np.random.uniform(0, 1)
                    if p >= (1 - brightness[2]):
                        batch__x[i] = brightness(batch__x[i], min_scale=brightness[0], max_scale=brightness[1])

                # Could easily be extended to also allow vertical flipping, but I'm not convinced of the
                # usefulness of vertical flipping either empirically or theoretically, so I'm going for simplicity.
                # If you want to allow vertical flipping, just change this function to pass the respective argument
                # to `_flip()`.
                if flip:
                    p = np.random.uniform(0, 1)
                    if p >= (1 - flip):
                        batch__x[i] = flip(batch__x[i])
                        batch_y[i][:, [xmin, xmax]] = img_width - batch_y[i][:, [xmax, xmin]]
                        # xmin and xmax are swapped when mirrored

                if scale:
                    p = np.random.uniform(0, 1)
                    if p >= (1 - scale[2]):
                        # Rescale the image and return the transformation matrix rot_mat so we can use it to adjust
                        # the box coordinates
                        batch__x[i], rot_mat, scale_factor = scale(batch__x[i], scale[0], scale[1])
                        # Adjust the box coordinates Transform two opposite corner points of the rectangular boxes
                        # using the transformation matrix `rot_mat`
                        top_lefts = np.array([batch_y[i][:, xmin], batch_y[i][:, ymin], np.ones(batch_y[i].shape[0])])
                        bottom_rights = np.array(
                            [batch_y[i][:, xmax], batch_y[i][:, ymax], np.ones(batch_y[i].shape[0])])
                        new_top_lefts = (np.dot(rot_mat, top_lefts)).T
                        new_bottom_rights = (np.dot(rot_mat, bottom_rights)).T
                        batch_y[i][:, [xmin, ymin]] = new_top_lefts.astype(np.int)
                        batch_y[i][:, [xmax, ymax]] = new_bottom_rights.astype(np.int)
                        # Limit the box coordinates to lie within the image boundaries
                        if limit_boxes and (
                                    scale_factor > 1):  # We don't need to do any limiting in case we shrunk the image
                            before_limiting = deepcopy(batch_y[i])
                            x_coords = batch_y[i][:, [xmin, xmax]]
                            x_coords[x_coords >= img_width] = img_width - 1
                            x_coords[x_coords < 0] = 0
                            batch_y[i][:, [xmin, xmax]] = x_coords
                            y_coords = batch_y[i][:, [ymin, ymax]]
                            y_coords[y_coords >= img_height] = img_height - 1
                            y_coords[y_coords < 0] = 0
                            batch_y[i][:, [ymin, ymax]] = y_coords
                            # Some objects might have gotten pushed so far outside the image boundaries in the
                            # transformation process that they don't serve as useful training examples anymore,
                            # because too little of them is visible. We'll remove all boxes that we had to limit so
                            # much that their area is less than `include_thresh` of the box area before limiting.
                            before_area = (before_limiting[:, xmax] - before_limiting[:, xmin]) * (
                                before_limiting[:, ymax] - before_limiting[:, ymin])
                            after_area = (batch_y[i][:, xmax] - batch_y[i][:, xmin]) * (
                                batch_y[i][:, ymax] - batch_y[i][:, ymin])
                            if include_thresh == 0:
                                batch_y[i] = batch_y[i][
                                    after_area > include_thresh * before_area]
                                # If `include_thresh == 0`, we want to make sure that boxes with area 0 get thrown
                                # out, hence the ">" sign instead of the ">=" sign
                            else:
                                batch_y[i] = batch_y[i][
                                    after_area >= include_thresh * before_area]
                                # Especially for the case `include_thresh == 1` we want the ">=" sign, otherwise no
                                # boxes would be left at all

                if random_crop:
                    # Compute how much room we have in both dimensions to make a random crop. A negative number here
                    # means that we want to crop out a patch that is larger than the original image in the respective
                    #  dimension, in which case we will create a black background canvas onto which we will randomly
                    # place the image.
                    y_range = img_height - random_crop[0]
                    x_range = img_width - random_crop[1]
                    # Keep track of the number of trials and of whether or not the most recent crop contains at least
                    #  one object
                    min_1_object_fulfilled = False
                    trial_counter = 0
                    while (not min_1_object_fulfilled) and (trial_counter < random_crop[3]):
                        # Select a random crop position from the possible crop positions
                        if y_range >= 0:
                            crop_ymin = np.random.randint(0,
                                                          y_range + 1)
                            # There are y_range + 1 possible positions for the crop in the vertical dimension
                        else:
                            crop_ymin = np.random.randint(0,
                                                          -y_range + 1)
                            # The possible positions for the image on the background canvas in the vertical dimension
                        if x_range >= 0:
                            crop_xmin = np.random.randint(0,
                                                          x_range + 1)
                            # There are x_range + 1 possible positions for the crop in the horizontal dimension
                        else:
                            crop_xmin = np.random.randint(0,
                                                          -x_range + 1)
                            # The possible positions for the image on the background canvas in the horizontal dimension
                        # Perform the crop
                        if y_range >= 0 and x_range >= 0:
                            # If the patch to be cropped out is smaller than the original image in both dimensions,
                            # we just perform a regular crop Crop the image
                            patch_x = np.copy(
                                batch__x[i][crop_ymin:crop_ymin + random_crop[0], crop_xmin:crop_xmin + random_crop[1]])
                            # Translate the box coordinates into the new coordinate system: Cropping shifts the
                            # origin by `(crop_ymin, crop_xmin)`
                            patch_y = np.copy(batch_y[i])
                            patch_y[:, [ymin, ymax]] -= crop_ymin
                            patch_y[:, [xmin, xmax]] -= crop_xmin
                            # Limit the box coordinates to lie within the new image boundaries
                            if limit_boxes:
                                # Both the x- and y-coordinates might need to be limited
                                before_limiting = np.copy(patch_y)
                                y_coords = patch_y[:, [ymin, ymax]]
                                y_coords[y_coords < 0] = 0
                                y_coords[y_coords >= random_crop[0]] = random_crop[0] - 1
                                patch_y[:, [ymin, ymax]] = y_coords
                                x_coords = patch_y[:, [xmin, xmax]]
                                x_coords[x_coords < 0] = 0
                                x_coords[x_coords >= random_crop[1]] = random_crop[1] - 1
                                patch_y[:, [xmin, xmax]] = x_coords
                        elif y_range >= 0 and x_range < 0:
                            # If the crop is larger than the original image in the horizontal dimension only,...
                            # Crop the image
                            patch_x = np.copy(batch__x[i][crop_ymin:crop_ymin + random_crop[0]])
                            # ...crop the vertical dimension just as before,...

                            canvas = np.zeros((random_crop[0], random_crop[1], patch_x.shape[2]),
                                              dtype=np.uint8)
                            # ...generate a blank background image to place the patch onto,...
                            canvas[:, crop_xmin:crop_xmin + img_width] = patch_x
                            # ...and place the patch onto the canvas at the random `crop_xmin` position computed above.
                            patch_x = canvas
                            # Translate the box coordinates into the new coordinate system: In this case, the origin
                            # is shifted by `(crop_ymin, -crop_xmin)`
                            patch_y = np.copy(batch_y[i])
                            patch_y[:, [ymin, ymax]] -= crop_ymin
                            patch_y[:, [xmin, xmax]] += crop_xmin
                            # Limit the box coordinates to lie within the new image boundaries
                            if limit_boxes:
                                # Only the y-coordinates might need to be limited
                                before_limiting = np.copy(patch_y)
                                y_coords = patch_y[:, [ymin, ymax]]
                                y_coords[y_coords < 0] = 0
                                y_coords[y_coords >= random_crop[0]] = random_crop[0] - 1
                                patch_y[:, [ymin, ymax]] = y_coords
                        elif y_range < 0 and x_range >= 0:
                            # If the crop is larger than the original image in the vertical dimension only,...
                            # Crop the image
                            patch_x = np.copy(batch__x[i][:, crop_xmin:crop_xmin + random_crop[
                                1]])  # ...crop the horizontal dimension just as in the first case,...
                            canvas = np.zeros((random_crop[0], random_crop[1], patch_x.shape[2]),
                                              dtype=np.uint8)
                            # ...generate a blank background image to place the patch onto,...
                            canvas[crop_ymin:crop_ymin + img_height, :] = patch_x
                            # ...and place the patch onto the canvas at the random `crop_ymin` position computed above.
                            patch_x = canvas
                            # Translate the box coordinates into the new coordinate system: In this case, the origin
                            # is shifted by `(-crop_ymin, crop_xmin)`
                            patch_y = np.copy(batch_y[i])
                            patch_y[:, [ymin, ymax]] += crop_ymin
                            patch_y[:, [xmin, xmax]] -= crop_xmin
                            # Limit the box coordinates to lie within the new image boundaries
                            if limit_boxes:
                                # Only the x-coordinates might need to be limited
                                before_limiting = np.copy(patch_y)
                                x_coords = patch_y[:, [xmin, xmax]]
                                x_coords[x_coords < 0] = 0
                                x_coords[x_coords >= random_crop[1]] = random_crop[1] - 1
                                patch_y[:, [xmin, xmax]] = x_coords
                        else:  # If the crop is larger than the original image in both dimensions,...
                            patch_x = np.copy(batch__x[i])
                            canvas = np.zeros((random_crop[0], random_crop[1], patch_x.shape[2]),
                                              dtype=np.uint8)
                            # ...generate a blank background image to place the patch onto,...
                            canvas[crop_ymin:crop_ymin + img_height, crop_xmin:crop_xmin + img_width] = patch_x
                            # ...and place the patch onto the canvas at the random `(crop_ymin, crop_xmin)` position
                            # computed above.
                            patch_x = canvas
                            # Translate the box coordinates into the new coordinate system: In this case, the origin
                            # is shifted by `(-crop_ymin, -crop_xmin)`
                            patch_y = np.copy(batch_y[i])
                            patch_y[:, [ymin, ymax]] += crop_ymin
                            patch_y[:, [xmin, xmax]] += crop_xmin
                            # Note that no limiting is necessary in this case
                        # Some objects might have gotten pushed so far outside the image boundaries in the
                        # transformation process that they don't serve as useful training examples anymore,
                        # because too little of them is visible. We'll remove all boxes that we had to limit so much
                        # that their area is less than `include_thresh` of the box area before limiting.
                        if limit_boxes and (y_range >= 0 or x_range >= 0):
                            before_area = (before_limiting[:, xmax] - before_limiting[:, xmin]) * (
                                before_limiting[:, ymax] - before_limiting[:, ymin])
                            after_area = (patch_y[:, xmax] - patch_y[:, xmin]) * (patch_y[:, ymax] - patch_y[:, ymin])
                            if include_thresh == 0:
                                patch_y = patch_y[
                                    after_area > include_thresh * before_area]
                                # If `include_thresh == 0`, we want to make sure that boxes with area 0 get thrown
                                # out, hence the ">" sign instead of the ">=" sign
                            else:
                                patch_y = patch_y[
                                    after_area >= include_thresh * before_area]
                                # Especially for the case `include_thresh == 1` we want the ">=" sign, otherwise no
                                # boxes would be left at all
                        trial_counter += 1  # We've just used one of our trials
                        # Check if we have found a valid crop
                        if random_crop[2] == 0:
                            # If `min_1_object == 0`, break out of the while loop after the first loop because we are
                            #  fine with whatever crop we got
                            batch__x[i] = patch_x  # The cropped patch becomes our new batch item
                            batch_y[i] = patch_y  # The adjusted boxes become our new labels for this batch item
                            # Update the image size so that subsequent transformations can work correctly
                            img_height = random_crop[0]
                            img_width = random_crop[1]
                            break
                        elif len(
                                patch_y) > 0:  # If we have at least one object left, this crop is valid and we can stop
                            min_1_object_fulfilled = True
                            batch__x[i] = patch_x  # The cropped patch becomes our new batch item
                            batch_y[i] = patch_y  # The adjusted boxes become our new labels for this batch item
                            # Update the image size so that subsequent transformations can work correctly
                            img_height = random_crop[0]
                            img_width = random_crop[1]
                        elif (trial_counter >= random_crop[3]) and (i not in batch_items_to_remove):
                            # If we've reached the trial limit and still not found a valid crop, remove this image
                            # from the batch
                            batch_items_to_remove.append(i)

                if crop:
                    # Crop the image
                    batch__x[i] = np.copy(batch__x[i][crop[0]:img_height - crop[1], crop[2]:img_width - crop[3]])
                    # Translate the box coordinates into the new coordinate system if necessary: The origin is
                    # shifted by `(crop[0], crop[2])` (i.e. by the top and left crop values) If nothing was cropped
                    # off from the top or left of the image, the coordinate system stays the same as before
                    if crop[0] > 0:
                        batch_y[i][:, [ymin, ymax]] -= crop[0]
                    if crop[2] > 0:
                        batch_y[i][:, [xmin, xmax]] -= crop[2]
                    # Update the image size so that subsequent transformations can work correctly
                    img_height -= crop[0] + crop[1]
                    img_width -= crop[2] + crop[3]
                    # Limit the box coordinates to lie within the new image boundaries
                    if limit_boxes:
                        before_limiting = np.copy(batch_y[i])
                        # We only need to check those box coordinates that could possibly have been affected by the
                        # cropping For example, if we only crop off the top and/or bottom of the image, there is no
                        # need to check the x-coordinates
                        if crop[0] > 0:
                            y_coords = batch_y[i][:, [ymin, ymax]]
                            y_coords[y_coords < 0] = 0
                            batch_y[i][:, [ymin, ymax]] = y_coords
                        if crop[1] > 0:
                            y_coords = batch_y[i][:, [ymin, ymax]]
                            y_coords[y_coords >= img_height] = img_height - 1
                            batch_y[i][:, [ymin, ymax]] = y_coords
                        if crop[2] > 0:
                            x_coords = batch_y[i][:, [xmin, xmax]]
                            x_coords[x_coords < 0] = 0
                            batch_y[i][:, [xmin, xmax]] = x_coords
                        if crop[3] > 0:
                            x_coords = batch_y[i][:, [xmin, xmax]]
                            x_coords[x_coords >= img_width] = img_width - 1
                            batch_y[i][:, [xmin, xmax]] = x_coords
                        # Some objects might have gotten pushed so far outside the image boundaries in the
                        # transformation process that they don't serve as useful training examples anymore,
                        # because too little of them is visible. We'll remove all boxes that we had to limit so much
                        # that their area is less than `include_thresh` of the box area before limiting.
                        before_area = (before_limiting[:, xmax] - before_limiting[:, xmin]) * (
                            before_limiting[:, ymax] - before_limiting[:, ymin])
                        after_area = (batch_y[i][:, xmax] - batch_y[i][:, xmin]) * (
                            batch_y[i][:, ymax] - batch_y[i][:, ymin])
                        if include_thresh == 0:
                            batch_y[i] = batch_y[i][
                                after_area > include_thresh * before_area]
                            # If `include_thresh == 0`, we want to make sure that boxes with area 0 get thrown out,
                            # hence the ">" sign instead of the ">=" sign
                        else:
                            batch_y[i] = batch_y[i][
                                after_area >= include_thresh * before_area]
                            # Especially for the case `include_thresh == 1` we want the ">=" sign, otherwise no boxes
                            #  would be left at all

                if resize:
                    batch__x[i] = cv2.resize(batch__x[i], dsize=resize)
                    batch_y[i][:, [xmin, xmax]] = (batch_y[i][:, [xmin, xmax]] * (resize[0] / img_width)).astype(np.int)
                    batch_y[i][:, [ymin, ymax]] = (batch_y[i][:, [ymin, ymax]] * (resize[1] / img_height)).astype(
                        np.int)

                if gray:
                    batch__x[i] = np.expand_dims(cv2.cvtColor(batch__x[i], cv2.COLOR_RGB2GRAY), 3)

            # If any batch items need to be removed because of failed random cropping, remove them now.
            for j in sorted(batch_items_to_remove, reverse=True):
                batch__x.pop(j)
                batch_y.pop(j)  # This isn't efficient, but this should hopefully not need to be done often anyway

            if train:  # During training we need the encoded labels instead of the format that `batch_y` has
                if ssd_box_encoder is None:
                    raise ValueError("`ssd_box_encoder` cannot be `None` in training mode.")
                y_true = ssd_box_encoder.encode_y(batch_y)
                # Encode the labels into the `y_true` tensor that the cost function needs

                # CAUTION: Converting `batch__x` into an array will result in an empty batch if the images have varying
                # sizes. At this point, all images have to have the same size, otherwise you will get an error during
                # training.
                # if train:
                # if diagnostics:
                #     yield (np.array(batch__x), y_true, batch_y, this_filenames, original_images, original_labels)
                # else:
                yield (np.array(batch__x), y_true)
            else:
                yield (np.array(batch__x), batch_y, this_filenames)

    def get_n_samples(self):
        """
        Returns:
            The number of image files in the initialized dataset.
        """
        return len(self.filenames)
