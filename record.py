import os
import time
import cv2
import argparse
import pyk4a
import json
import numpy as np
import Jetson.GPIO as GPIO
import logging
from datetime import datetime
import queue
import threading

class AzureKinectRecorder:
    def __init__(self, fn, camera_mode, binned, resolution, fps):
        self.fn = fn
        self.depth_mode = self.get_depth_mode(camera_mode, binned)
        self.resolution = self.get_color_resolution(resolution)
        self.fps = self.get_fps(fps)
        
        # Initialize logging
        logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
        
        self.camera = pyk4a.PyK4A(
            pyk4a.Config(
                color_resolution=self.resolution,
                depth_mode=self.depth_mode,
                camera_fps=self.fps,
                color_format=pyk4a.ImageFormat.COLOR_BGRA32,
            )
        )
        self.camera.start()
        
        # Log the configuration details in a structured manner
        logging.info('Camera Configuration:')
        logging.info(f'    Color Resolution: {resolution}p')
        logging.info(f'    Camera FOV Mode: {camera_mode.upper()}')
        logging.info(f'    Binned Mode: {"Enabled" if binned else "Disabled"}')
        logging.info(f'    FPS: {fps}')
        logging.info('Ready!')

        # Initialize frame queue and stop event
        self.save_thread = None
        self.frame_queue = queue.Queue()
        self.stop_event = threading.Event()
    
    def write_intrinsic_matrix(self, out_dir, fn="intrinsic"):
        tof_intrinsic_array = self.camera.calibration.get_camera_matrix(pyk4a.calibration.CalibrationType.DEPTH)
        tof_intrinsic_list = tof_intrinsic_array.flatten().tolist()

        tof_distortion_array = self.camera.calibration.get_distortion_coefficients(pyk4a.CalibrationType.DEPTH)
        tof_distortion_list = tof_distortion_array.flatten().tolist()

        data = {"intrinsic_matrix": tof_intrinsic_list, "distortion_coefficients": tof_distortion_list}
        tmp_path = os.path.join(out_dir, f'{fn}.json')
        with open(tmp_path, 'w') as f:
            json.dump(data, f)

    @staticmethod
    def get_depth_mode(camera_mode, binned):
        if camera_mode.lower() == "wfov":
            return pyk4a.DepthMode.WFOV_2X2BINNED if binned else pyk4a.DepthMode.WFOV_UNBINNED
        elif camera_mode.lower() == "nfov":
            return pyk4a.DepthMode.NFOV_2X2BINNED if binned else pyk4a.DepthMode.NFOV_UNBINNED

    @staticmethod
    def get_color_resolution(resolution):
        resolution_map = {
            3072: pyk4a.ColorResolution.RES_3072P,
            2160: pyk4a.ColorResolution.RES_2160P,
            1536: pyk4a.ColorResolution.RES_1536P,
            1440: pyk4a.ColorResolution.RES_1440P,
            1080: pyk4a.ColorResolution.RES_1080P,
            720: pyk4a.ColorResolution.RES_720P,
        }
        return resolution_map.get(resolution, pyk4a.ColorResolution.RES_3072P)

    @staticmethod
    def get_fps(fps):
        fps_map = {
            30: pyk4a.FPS.FPS_30,
            15: pyk4a.FPS.FPS_15,
            5: pyk4a.FPS.FPS_5,
        }
        return fps_map.get(fps, pyk4a.FPS.FPS_15)

    def create_directory(self, path):
        if not os.path.exists(path):
            os.makedirs(path)

    def capture_frame(self, capture, frame_count):
        folders = ['color', 'transformed_color', 'depth', 'transformed_depth', 'ir']
        for folder in folders:
            tmp_path = os.path.join(self.out_dir, folder)
            if not os.path.exists(tmp_path):
                self.create_directory(os.path.join(self.out_dir, folder))

        frame_types = {
            'color': capture.color,
            'transformed_color': capture.transformed_color,
            'depth': capture.depth,
            'transformed_depth': capture.transformed_depth,
            'ir': capture.ir,
        }

        try:
            for frame_type, frame in frame_types.items():
                file_path = os.path.join(self.out_dir, frame_type, f'{frame_type}_{frame_count:03}.png')
                cv2.imwrite(file_path, frame)

            logging.info(f'Frame {frame_count} Captured')
        except Exception as e:
            logging.error(f'Error capturing frame: {e}')

    def save_frames_worker(self):
        while not self.stop_event.is_set() or not self.frame_queue.empty():
            try:
                frame_data = self.frame_queue.get(timeout=1)  # Adjust timeout as needed
                self.capture_frame(*frame_data)
            except queue.Empty:
                continue

    def run(self):
        GPIO.setmode(GPIO.BOARD)
        GPIO.setup(15, GPIO.IN)
        GPIO.setup(16, GPIO.IN)
        
        session_active = False
        timestamp = None
        
        # Start the save frames thread
        if self.save_thread is None:
            self.save_thread = threading.Thread(target=self.save_frames_worker)
            self.save_thread.start()

        try:
            while True:
                if GPIO.input(15) == GPIO.LOW and not session_active:
                    timestamp = datetime.now().strftime('%y-%m-%d_%H-%M')
                    self.out_dir = os.path.join(self.fn, timestamp)
                    self.create_directory(self.out_dir)

                    self.write_intrinsic_matrix(self.out_dir)
                    
                    frame_count = 0
                    
                    logging.info(f'Session Started {timestamp}')
                    
                    last_state_pin16 = GPIO.input(16)
                    session_active = True

                    while GPIO.input(15) == GPIO.LOW:
                        current_state_pin16 = GPIO.input(16)
                        if last_state_pin16 == GPIO.HIGH and current_state_pin16 == GPIO.LOW:
                            capture = self.camera.get_capture()
                            if capture:
                                self.frame_queue.put((capture, frame_count))
                                frame_count += 1
                        last_state_pin16 = current_state_pin16
                        time.sleep(0.01)

                elif session_active:
                    logging.info(f'Session Ended {timestamp}')
                    session_active = False
                    
                time.sleep(0.01)
        except KeyboardInterrupt:
            logging.info('Recording interrupted by user!')
        except Exception as e:
            logging.error(f'Unexpected error occurred: {e}')
        finally:
            self.end()

    def end(self):
        self.stop_event.set()
        if self.save_thread is not None:
            self.save_thread.join()
        self.camera.stop()
        GPIO.cleanup()
        logging.info('Camera Stopped and GPIO Cleaned')

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Azure Kinect Recorder.')
    parser.add_argument('--fn', default='capture', help='Filename for saving the capture. If not provided, "capture" will be used.')
    parser.add_argument('--camera_mode', default='nfov', choices=['wfov', 'nfov'], help='Camera FOV Mode [Default = nfov]')
    parser.add_argument('--binned', action='store_true', help='2x2 Sensor Binning [Default = False]')
    parser.add_argument('--resolution', type=int, default=3072, help='Color Image Resolution [Default = 3072]')
    parser.add_argument('--fps', type=int, default=15, help='Frames per Second [Default = 15]')
    
    args = parser.parse_args()

    recorder = AzureKinectRecorder(args.fn, args.camera_mode, args.binned, args.resolution, args.fps)
    recorder.run()