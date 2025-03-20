from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.support.ui import Select, WebDriverWait
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import NoSuchElementException
from webdriver_manager.chrome import ChromeDriverManager
import os
import pandas as pd
import concurrent.futures
import threading
from datetime import datetime
from django.core.management.base import BaseCommand
from django.core.mail import EmailMessage
from users.models import CustomUser
from django.conf import settings
from django.db.models import Q
from django.core.management import call_command
import shutil
from leads_data.models import DailySheet
import sys

class Command(BaseCommand):
    help = "Runs the daily MC scrape, processes data, and sends reports"

    def handle(self, *args, **kwargs):
        print("Starting up")

        # Initialize WebDriver Service using WebDriver Manager
        service = Service(ChromeDriverManager().install())
        options = Options()
        # options.add_argument('--headless')
        options.add_argument('--no-sandbox')
        options.add_argument('--disable-dev-shm-usage')

        # Create WebDriver instance
        driver = webdriver.Chrome(service=service, options=options)

        # Thread-local storage for WebDriver
        driver_storage = threading.local()

        ################################################## Get the list of New Mc numbers ##################################################

        parent_url = "https://li-public.fmcsa.dot.gov/LIVIEW/pkg_carrquery.prc_carrlist?n_dotno=2952744&s_prefix=MC&n_docketno=&s_legalname=&s_dbaname=&s_state="

        driver.get(parent_url)

        # Wait for the dropdown to be visible
        wait = WebDriverWait(driver, 15)  # Wait up to 10 seconds
        dropdown_element = wait.until(EC.visibility_of_element_located((By.NAME, "pv_choice")))

        # Wrap the WebElement in a Select object
        dropdown = Select(dropdown_element)

        # Select "FMCSA Register" by value
        dropdown.select_by_value("FED_REG")

        # Find and click the "Go" button (input type="image")
        go_button = driver.find_element(By.XPATH, '//input[@type="image"]')

        # Submit the form by clicking the "Go" button
        go_button.click()

        # Locate the table
        table = WebDriverWait(driver, 10).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "table[summary='Table used for formatting purpose only']"))
        )

        # compare today's date with the date written, if it's not today's date stop the code then and there
        # Get the first date from the table
        first_date_element = table.find_element(By.XPATH, ".//tr[2]/th")
        first_date_text = first_date_element.text.strip()
        print("Extracted date text:", first_date_text)

        # Validate that we got a real date, not a header like 'Date'
        if first_date_text.lower() == "date" or not first_date_text:
            raise ValueError("Failed to extract a valid date from the table!")

        # Convert to datetime object
        table_date = datetime.strptime(first_date_text, "%m/%d/%Y").date()

        # Get today's date
        today_date = datetime.today().date()

        # Compare dates
        if table_date != today_date:
            print(f"Date mismatch! Found {table_date}, but expected {today_date}. Exiting script.")
            return
        else:
            print(f"Date matches: {table_date}")

        # Find the first form inside the table (HTML Detail button)
        first_html_detail_button = table.find_element(By.XPATH, ".//form/input[@type='submit']")

        # Click the button
        first_html_detail_button.click()

        # Find the table that comes next after the "CERTIFICATE, PERMIT, LICENSE" anchor
        target_table = wait.until(
            EC.presence_of_element_located((By.XPATH, "//a[@name='CPL']/following::table[1]"))
        )

        # Initialize an empty list to store the extracted numbers
        mc_numbers_list = []

        # Iterate over all the rows of the table
        for row in target_table.find_elements(By.TAG_NAME, "tr"):
            # Find the first 'th' element in each row (which contains the MC number)
            th_element = row.find_element(By.TAG_NAME, "th")
            mc_number_text = th_element.text.strip()  # Get the text from the 'th' element

            # Check if the MC number ends with "-C"
            if mc_number_text.endswith("-C"):
                # Extract only the numeric part by splitting on the dash
                mc_number = mc_number_text.split("-")[1].strip()  # This will extract the "362051" part
                mc_numbers_list.append(mc_number)  # Add to the list

        # Print or return the extracted MC numbers

        # Initialize 4 empty mini-lists
        mini_lists = [[], [], [], []]

        # Distribute the elements one by one into the mini-lists
        for i, entry in enumerate(mc_numbers_list):
            mini_lists[i % 4].append(entry)

        driver.quit()

        for x in mini_lists:
            print(len(x))

        ############################################################ Sent them to the mC scrape code to be extracted



        driver_storage = threading.local()
        print_lock = threading.Lock()

        def get_driver():
            """Ensures each thread gets its own WebDriver instance."""
            if not hasattr(driver_storage, 'driver'):
                service = Service(ChromeDriverManager().install())
                options = Options()
                options.add_argument('--headless')
                options.add_argument('--no-sandbox')
                options.add_argument('--disable-dev-shm-usage')
                driver_storage.driver = webdriver.Chrome(service=service, options=options)
            return driver_storage.driver

        def write_to_excel(data, range_set_index):
            
            file_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), f"../../../data/new_mc_sheets/mc_data_{range_set_index}.xlsx")

            # Check if the file already exists
            if os.path.exists(file_path):
                existing_data = pd.read_excel(file_path)
                df = pd.concat([existing_data, pd.DataFrame(data)], ignore_index=True)
            else:
                df = pd.DataFrame(data)

            # Write the data to the Excel file
            df.to_excel(file_path, index=False)
            return df

        def process_mc_range(mc_list, range_set_index):
            driver = get_driver()

            wait = WebDriverWait(driver, 20)
            mc_list_url = "https://safer.fmcsa.dot.gov/CompanySnapshot.aspx"

            # Navigate to the page you want to scrape
            driver.get(mc_list_url)
            data = []

            # Select the 'MC/MX Number' option using its value attribute
            # mc_mx_radio = driver.find_element(By.CSS_SELECTOR, "input[value='MC_MX']")
            mc_mx_radio = wait.until(EC.element_to_be_clickable(
                (By.CSS_SELECTOR, "input[value='MC_MX']")))
            mc_mx_radio.click()
            
            skipped_mc_counter = 0
            mc_number_counter = 1 # To count the position of the mc number in its list
            loop_counter = 1 # To keep track of the iteration
            
            print(f"Mc list of {range_set_index}: \n {len(mc_list)}")
            
            try:
                for mc_number in mc_list:

                    if driver.session_id is None:
                        print(f"WebDriver session is lost. Terminating loop. List {range_set_index}")
                        break
                    
                    with print_lock:
                      print(f"Processing MC Number: {mc_number:07}. Number {mc_number_counter} in list {range_set_index}")

                    # Clear the previous input in the input field before entering a new MC number
                    query_input = wait.until(EC.presence_of_element_located(
                        (By.CSS_SELECTOR, "input[name='query_string']")))
                    query_input.clear()
                    # Input the current MC number with leading zeros
                    query_input.send_keys(f"{mc_number}")

                    # Use Ctrl + Click to open the result in a new tab
                    try:
                        search_button = wait.until(EC.element_to_be_clickable(
                          (By.CSS_SELECTOR, "input[type='SUBMIT']")))
                        search_button.send_keys(Keys.CONTROL, Keys.ENTER)
                    except:
                        with print_lock:
                          print(f"MC Number {mc_number}: Timed out waiting. Skipping this entry.")
                        # Close the current tab and move on to the next one
                        driver.close()
                        if len(driver.window_handles) > 0:
                            driver.switch_to.window(driver.window_handles[0])
                        continue  # Continue to the next MC number
                    
                    # Switch to the new tab
                    wait.until(lambda d: len(driver.window_handles) > 1)  # Ensure the tab is open before switching
                    driver.switch_to.window(driver.window_handles[-1])  # Always switch to the last opened tab


                    # Print the title of the new page
                    try:
                        wait.until(lambda d: len(driver.window_handles) > 1)
                    except:
                        with print_lock:
                          print(f"MC Number {mc_number}: Timed out waiting for the legal name. Skipping this entry.")
                        # Close the current tab and move on to the next one
                        driver.close()
                        if len(driver.window_handles) > 0:
                            driver.switch_to.window(driver.window_handles[0])
                        continue  # Continue to the next MC number
                    
                    try:
                        # Search for the specific text or structure in the table
                        table_element = driver.find_element(
                            By.XPATH, "//b[contains(text(), 'Other Information for this Carrier')]")

                        # If the table is found, Click the "SMS Results" link
                        sms_results_link = driver.find_element(By.LINK_TEXT, "SMS Results")
                        sms_results_link.click()

                        if driver.title == "Safety Measurement System - Simple Search":
                            skipped_mc_counter += 1
                            driver.switch_to.window(driver.window_handles[1])
                            driver.close()
                            driver.switch_to.window(driver.window_handles[0])

                            with print_lock:
                              print(f"MC Number {mc_number} Skipped. Total number of skipped numbers in list {range_set_index}: {skipped_mc_counter}")
                            continue

                        try:
                          # Wait for the new page to load by waiting for a specific element on that page to be present
                          wait.until(EC.presence_of_element_located(
                              (By.LINK_TEXT, "Carrier Registration Details")))

                          # Click the "Carrier Registration Details" link
                          carrier_registration_link = wait.until(EC.element_to_be_clickable(
                              (By.LINK_TEXT, "Carrier Registration Details")))
                        except:
                          with print_lock:
                            print(f"MC Number {mc_number}: Timed out waiting for the legal name. Skipping this entry.")
                          # Close the current tab and move on to the next one
                          driver.close()
                          if len(driver.window_handles) > 0:
                              driver.switch_to.window(driver.window_handles[0])
                          continue  # Continue to the next MC number

                        try:
                          carrier_registration_link.click()
                        except:
                            # Optionally, wait or scroll again before retrying
                            driver.execute_script(
                                "arguments[0].scrollIntoView();", carrier_registration_link)
                            try:
                              carrier_registration_link.click()  # Retry clicking the link
                            except:
                              driver.switch_to.window(driver.window_handles[-1])  # Ensure switching to the last opened tab before closing
                              driver.close()
                              if len(driver.window_handles) > 0:
                                  driver.switch_to.window(driver.window_handles[0])
                              with print_lock:
                                print(f"MC Number {mc_number} Skipped")
                              continue

                        # Find the values in the `ul` with class 'col1'
                        try:
                            legal_name = wait.until(EC.presence_of_element_located(
                            (By.XPATH, "//ul[@class='col1']//li[label[contains(text(),'Legal Name:')]]/span"))).text
                        except:
                          print(f"MC Number {mc_number}: Timed out waiting for the legal name. Skipping this entry.")
                          # Close the current tab and move on to the next one
                          driver.close()
                          if len(driver.window_handles) > 0:
                              driver.switch_to.window(driver.window_handles[0])
                          continue  # Continue to the next MC number

                        telephone = driver.find_element(
                            By.XPATH, "//ul[@class='col1']//li[label[contains(text(),'Telephone:')]]/span").text
                        email = driver.find_element(
                            By.XPATH, "//ul[@class='col1']//li[label[contains(text(),'Email:')]]/span").text
                        address = driver.find_element(
                            By.XPATH, "//ul[@class='col1']//li[label[contains(text(),'Address:')]]/span").text
                        us_dot = driver.find_element(
                            By.XPATH, "//ul[@class='col1']//li[label[contains(text(),'U.S. DOT#:')]]/span").text

                        # Extracting the additional details from the 'col2' section
                        vehicle_miles_traveled = driver.find_element(
                            By.XPATH, "//ul[@class='col2']//li[label[contains(text(),'Vehicle Miles Traveled:')]]/span").text
                        vmt_year = driver.find_element(
                            By.XPATH, "//ul[@class='col2']//li[label[contains(text(),'VMT Year:')]]/span").text
                        power_units = driver.find_element(
                            By.XPATH, "//ul[@class='col2']//li[label[contains(text(),'Power Units:')]]/span").text
                        duns_number = driver.find_element(
                            By.XPATH, "//ul[@class='col2']//li[label[contains(text(),'DUNS Number:')]]/span").text
                        drivers = driver.find_element(
                            By.XPATH, "//ul[@class='col2']//li[label[contains(text(),'Drivers:')]]/span").text
                        carrier_operation = driver.find_element(
                            By.XPATH, "//ul[@class='col2']//li[label[contains(text(),'Carrier Operation:')]]/span").text
                        passenger = driver.find_element(
                            By.XPATH, "//ul[@class='col2']//li[label[contains(text(),'Passenger:')]]/span").text
                        hm = driver.find_element(
                            By.XPATH, "//ul[@class='col2']//li[label[contains(text(),'HM:')]]/span").text
                        hhg = driver.find_element(
                            By.XPATH, "//ul[@class='col2']//li[label[contains(text(),'HHG:')]]/span").text
                        new_entrant = driver.find_element(
                            By.XPATH, "//ul[@class='col2']//li[label[contains(text(),'New Entrant:')]]/span").text

                        # Operation Confirmation
                        operation_classification = driver.find_element(
                            By.XPATH, "//ul[@class='opClass']//li[contains(@class,'checked')]").text.split('X')[1].strip()
                        
                        # Cargo Info
                        # Initialize an empty list to hold the extracted information
                        cargo_info = []

                        # Locate the table and iterate through its rows
                        vehicle_table = driver.find_element(By.XPATH, "//table[contains(., 'Vehicle Type')]")
                        rows = vehicle_table.find_elements(By.TAG_NAME, "tr")[1:]  # Skip the header row
                        
                        for row in rows:
                            vehicle_type = row.find_element(By.CLASS_NAME, "vehType").text
                            owned = int(row.find_elements(By.TAG_NAME, "td")[0].text.replace(',', ''))
                            term_leased = int(row.find_elements(By.TAG_NAME, "td")[1].text.replace(',', ''))
                            trip_leased = int(row.find_elements(By.TAG_NAME, "td")[2].text.replace(',', ''))


                            # Check each category for values greater than 0
                            if owned > 0:
                                cargo_info.append(f"{vehicle_type}: Owned - {owned}")
                            if term_leased > 0:
                                cargo_info.append(f"{vehicle_type}: Term Leased - {term_leased}")
                            if trip_leased > 0:
                                cargo_info.append(f"{vehicle_type}: Trip Leased - {trip_leased}")
                            
                        # Join all collected information into a single string
                        cargo_info_single_row = ", ".join(cargo_info)

                        # Cargo info
                        cargo_elements = driver.find_elements(
                            By.XPATH, "//ul[@class='cargo']//li[contains(@class, 'checked')]")
                        cargo_classifications = ', '.join(
                            [elem.text.split('X')[1].strip(
                            ) if 'X' in elem.text else 'Unknown' for elem in cargo_elements]
                        )
                        
                        driver.back()
                        usd_status_element = driver.find_element(By.XPATH, "//th[contains(., 'USDOT Status:')]/following-sibling::td")
                        usd_status_value = usd_status_element.text.strip()

                        # Print the extracted information
                        with print_lock:
                          print(f"Data Extracted for MC Number {mc_number}")

                        data.append({
													'MC Number': f"MC {mc_number:07}",
													'USDOT Status': usd_status_value,
													'Legal Name': legal_name,
													'Telephone': telephone,
													'Email': email,
													'Address': address,
													'U.S DOT': us_dot,
													'Vehicle Miles Traveled': vehicle_miles_traveled,
													'VMT Year': vmt_year,
													'Power Units': power_units,
													'DUNS Number': duns_number,
													'Drivers': drivers,
													'Carrier Operation': carrier_operation,
													'Passenger': passenger,
													'HM': hm,
													'HHG': hhg,
													'New Entrant': new_entrant,
													'Operation Classification': operation_classification,
													'Cargo Classifications': cargo_classifications,
													'Cargo Info': cargo_info_single_row
												})

                        # Close the tabs after processing, except the original tab
                        driver.close()  # Close the Carrier tab
                        # Switch back to the original tab
                        driver.switch_to.window(driver.window_handles[0])

                    except NoSuchElementException:
                        print(
                            f"MC Number {mc_number}: The required elements were not found.")
                        # Close the newly opened tabs before proceeding to the next iteration
                        if len(driver.window_handles) > 0:
                            driver.switch_to.window(driver.window_handles[1])
                            driver.close()
                        driver.switch_to.window(driver.window_handles[0])

                    if loop_counter%10 == 0 and len(data) != 0:
                        print(f"\nWRITING TO EXCEL FOR RANGE SET NUMBER {range_set_index}. {len(data)} NEW ENTRIES\n")
                        write_to_excel(data, range_set_index)
                        data.clear()

                    loop_counter += 1
                    mc_number_counter += 1
            except Exception as e:
                print(f"Unexpected fatal error: {e} for list {range_set_index}")


            if len(data) > 0:
                with print_lock:
                    print(f"\nFINAL WRITE TO EXCEL FOR RANGE SET NUMBER {range_set_index}. {len(data)} NEW ENTRIES\n")
                write_to_excel(data, range_set_index)
                
            with print_lock:
                print(f"Totla skipped mc numbers in list {range_set_index}: {skipped_mc_counter}")
                
            driver.quit()


        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
            # Submit tasks for each range and store the futures
            futures = [
                executor.submit(process_mc_range, mini_list, index) 
                for index, mini_list in enumerate(mini_lists, start=1)
            ]



        # Define the main folder path
        folder_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "../../../data/new_mc_sheets")

        # Ensure the folder exists
        os.makedirs(folder_path, exist_ok=True)

        # List to hold all the dataframes
        dataframes = []

        # Iterate over each file in the folder
        for filename in os.listdir(folder_path):
            if filename.startswith('mc_data_') and filename.endswith('.xlsx'):
                file_path = os.path.join(folder_path, filename)
                try:
                    df = pd.read_excel(file_path)
                    dataframes.append(df)
                except Exception as e:
                    print(f"❌ Error reading {filename}: {e}")

        merged_df = pd.concat(dataframes, ignore_index=True)

        # Define output file path
        filename = datetime.today().strftime('%Y-%m-%d') + '.xlsx'
        output_file = os.path.join(folder_path, filename)

        # Write the combined dataframe to a new Excel file
        merged_df.to_excel(output_file, index=False)
        print(f"📄 Merged Excel file saved as {output_file}")

        print("🚀 Importing data into the Lead model...")
        call_command("import_leads", str(output_file))
        print("✅ Data import completed!")

        # Function to send email
        def send_email(recipient_email):
            subject = "Daily MC Sheets - Uploaded on FMCSA"
            message = "Attached is the daily MC file."
            email = EmailMessage(subject, message, settings.EMAIL_HOST_USER, [recipient_email])
            email.attach_file(output_file)
            try:
                email.send()
                print(f"📧 Email sent to {recipient_email}")
            except Exception as e:
                print(f"❌ Failed to send email to {recipient_email}: {e}")

        # Fetch user emails where subscription status is active
        active_users = CustomUser.objects.filter(Q(subscription__status="active") | Q(on_free_trial=True)).distinct()
        user_emails = list(active_users.values_list("email", flat=True))

        if user_emails:
            # Send emails in a separate thread using ThreadPoolExecutor
            with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
                executor.map(send_email, user_emails)
        else:
            print("⚠️ No active subscribers found to send emails.")


        destination_folder = os.path.join(settings.MEDIA_ROOT, "daily_sheets")
        os.makedirs(destination_folder, exist_ok=True)  # Ensure destination exists
        destination_file = os.path.join(destination_folder, filename)
        shutil.move(output_file, destination_file)

        # Step 3: Save to the model with the correct path
        daily_sheet = DailySheet(file=f"daily_sheets/{filename}")  # Only relative path
        daily_sheet.save()
        print(f"✅ File saved in model: {daily_sheet.file.url}")


        # Delete the original files
        for filename in os.listdir(folder_path):
            if filename.startswith('mc_data_') and filename.endswith('.xlsx'):
                file_path = os.path.join(folder_path, filename)
                os.remove(file_path)
                print(f"🗑️ Deleted: {file_path}")

