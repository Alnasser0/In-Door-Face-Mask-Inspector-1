# -*- encoding=utf-8 -*-
# Took and modified the BaseCamera class, and changed the Camera Class
# Original Source: https://blog.miguelgrinberg.com/post/flask-video-streaming-revisited
# Original Code Repository: https://github.com/miguelgrinberg/flask-video-streaming
# Modified a little bit in inference method from face mask detection repository
# Code of inference: https://github.com/AIZOOTech/FaceMaskDetection
import io
import cv2
import time
import uuid
import gridfs
import config
import numpy as np
from smtplib import SMTP
from datetime import datetime
from threading import Thread, Timer
from email.mime.text import MIMEText
from email.mime.image import MIMEImage
from model.load_model import tf_inference
from email.mime.multipart import MIMEMultipart
from model.utils import decode_bbox, single_class_non_max_suppression 


ALERT_TRIGGER = dict()   # Used for fixing a time period to the alert


class BaseCamera(object):
    threads = dict()  # background thread that reads frames from camera
    frames = dict()  # current frame is stored here by background thread
    sources = dict()

    def __init__(self, _id, source):
        """Start the background camera thread if it isn't running yet."""
        self._id = str(_id)
        self.sources[self._id] = source
        if BaseCamera.threads.get(self._id) is None:
            # start background frame thread
            BaseCamera.threads[self._id] = Thread(
                target=self._thread, 
                args=(self._id, self.sources[self._id])
            )
            BaseCamera.threads[self._id].start()

            # wait until frames are available
            while self.get_frame() is None:
                time.sleep(0)

    def get_frame(self):
        """Return the current camera frame."""
        try:
            return BaseCamera.frames[self._id]
        except:    
            return None

    @staticmethod
    def _frames(_id, source):
        """"Generator that returns frames from the camera."""
        raise RuntimeError('Must be implemented by subclasses.')

    @classmethod
    def _thread(cls, _id, source):
        """Camera background thread."""
        print('Starting camera thread.')
        frames_iterator = cls._frames(_id, source)
        for frame in frames_iterator:
            BaseCamera.frames[_id] = frame
            time.sleep(0)


class Camera(BaseCamera):
    def __init__(self, _id):
        cam = config.db['cameras'].find_one({'_id': _id})
        super(Camera, self).__init__(_id, cam['url'])

    @staticmethod
    def _frames(_id, source):
        global ALERT_TRIGGER
        camera = cv2.VideoCapture(source)
        ALERT_TRIGGER[_id] = False
        status = True

        if camera.isOpened():
            while status:
                (status, img) = camera.read()
                if status:
                    settings = config.db['settings'].find_one({"id": 0})
                    has_violation = inference(img,
                                              settings['confidence'],
                                              iou_thresh=0.5,
                                              target_shape=(260, 260),
                                              draw_result=True
                                              )

                    jpg = cv2.imencode('.jpg', img)[1]
                    if has_violation and not ALERT_TRIGGER[_id]:
                        ALERT_TRIGGER[_id] = True
                        _trigger = Timer(settings['lock_duration'], trigger, args=(_id,))
                        _trigger.start()
                        _alert = Thread(target=alert, args=(jpg, _id))
                        _alert.start()

                    io_buf = io.BytesIO(jpg)
                    frame = io_buf.getvalue()
                    io_buf.close()
                    yield frame


def inference(image,
              conf_thresh=0.5,
              iou_thresh=0.4,
              target_shape=(160, 160),
              draw_result=True
              ):
    '''
    Main function of detection inference
    :param image: 3D numpy array of image
    :param conf_thresh: the min threshold of classification probabity.
    :param iou_thresh: the IOU threshold of NMS
    :param target_shape: the model input size.
    :param draw_result: whether to daw bounding box to the image.
    :param show_result: whether to display the image.
    :return:
    '''
    has_violation = False
    height, width, _ = image.shape
    image_resized = cv2.resize(image, target_shape)
    image_np = image_resized / 255.0
    image_exp = np.expand_dims(image_np, axis=0)
    y_bboxes_output, y_cls_output = tf_inference(
        config.SESS, config.GRAPH, image_exp
        )

    # remove the batch dimension, for batch is always 1 for inference.
    y_bboxes = decode_bbox(config.ANCHORS_EXP, y_bboxes_output)[0]
    y_cls = y_cls_output[0]
    # To speed up, do single class NMS, not multiple classes NMS.
    bbox_max_scores = np.max(y_cls, axis=1)
    bbox_max_score_classes = np.argmax(y_cls, axis=1)

    # keep_idx is the alive bounding box after nms.
    keep_idxs = single_class_non_max_suppression(y_bboxes,
                                                 bbox_max_scores,
                                                 conf_thresh=conf_thresh,
                                                 iou_thresh=iou_thresh,
                                                 )

    for idx in keep_idxs:
        conf = float(bbox_max_scores[idx])
        class_id = bbox_max_score_classes[idx]
        bbox = y_bboxes[idx]
        # clip the coordinate, avoid the value exceed the image boundary.
        xmin = max(0, int(bbox[0] * width))
        ymin = max(0, int(bbox[1] * height))
        xmax = min(int(bbox[2] * width), width)
        ymax = min(int(bbox[3] * height), height)

        if draw_result:
            if class_id == 0:
                color = (0, 255, 0)
            else:
                has_violation = True
                color = (0, 0, 255)
            cv2.rectangle(image, (xmin, ymin), (xmax, ymax), color, 2)
            cv2.putText(image, "%s: %.2f" % (config.ID2CLASS[class_id], conf), 
                                            (xmin + 2, ymin - 2),
                                            cv2.FONT_HERSHEY_SIMPLEX, 0.8, 
                                            color
                                            )
    return has_violation                                        


def trigger(_id):
    global ALERT_TRIGGER
    ALERT_TRIGGER[_id] = False


def alert(image, _id):
    cam = config.db['cameras'].find_one({'_id': uuid.UUID(_id)})
    if cam['supervisor_id'] != -1:
        sec = config.db['security'].find_one({
            '_id': uuid.UUID(cam['supervisor_id'])
            })
        send_mail(sec, cam, image)
    store_image(cam, image)


def send_mail(receiver, camera, image):
    msg = MIMEMultipart()
    msg['Subject'] = 'IDFMI Alert'
    msg['From'] = config.EMAIL_ADDRESS
    msg['To'] = receiver['email']
    msg.attach(MIMEText(
        f"""Dear Mr. {receiver['last_name']},

        We have detected a violation on {camera['location']}. 
        You can find the violation picture attached with this email. 
        Please check the picture.

        regards,
        IDFMI Team"""
        ))
    msg.attach(MIMEImage(image.tostring(), name="ViolationPic.jpg"))

    smtp = SMTP(config.SMTP_SERVER, config.SMTP_PORT)
    smtp.ehlo()
    smtp.starttls()
    smtp.ehlo()
    smtp.login(config.EMAIL_ADDRESS, config.EMAIL_PASSWORD)
    smtp.sendmail(config.EMAIL_ADDRESS, receiver['email'], msg.as_string())
    smtp.quit()


def store_image(camera, image):
    # searching for the location of the camera
    roomName = camera['location']
    fs = gridfs.GridFS(config.db)  # gridFS instance
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")  # get time

    # Converting the picture to bytes
    io_buf = io.BytesIO(image)
    frame = io_buf.getvalue()
    io_buf.close()

    fs.put(frame, date=now, room=roomName)  # save image into DB.
