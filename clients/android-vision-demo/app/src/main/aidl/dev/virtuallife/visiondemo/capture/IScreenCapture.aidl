package dev.virtuallife.visiondemo.capture;
import android.os.ParcelFileDescriptor;

// Only this fixed operation is exposed. PNG bytes never enter a Binder parcel.
interface IScreenCapture {
    ParcelFileDescriptor capture() = 0;
    void destroy() = 16777114;
}
