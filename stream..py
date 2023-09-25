import cv2
import cvzone

cap = cv2.VideoCapture(0)


ret, frame = cap.read()
y_size, x_size = frame.shape[:2]

def get_scorebug(old_scorebug=None):
    scorebug = cv2.imread('scorebug.png', cv2.IMREAD_UNCHANGED)
    if not hasattr(scorebug, 'size'):
        return old_scorebug
    y_base_size, x_base_size = scorebug.shape[:2]
    ratio = (x_size/x_base_size)/4
    scorebug = cv2.resize(scorebug, None, fx=ratio, fy=ratio)
    return scorebug

scorebug = get_scorebug()
y_base_size, x_base_size = scorebug.shape[:2]

while cap.isOpened():
    ret, frame = cap.read()
    scorebug = get_scorebug(old_scorebug=scorebug)
    image = cvzone.overlayPNG(frame, scorebug, [x_size - x_base_size-20, y_size - y_base_size-20])
    cv2.imshow('coucou', image)
    if cv2.waitKey(25) & 0xFF == ord('q'):
        break

cap.release()
cv2.destroyAllWindows()
